"""Authentication (TRZ-09, design 11.4): a one-time code by identity document, test credentials
for analysts, and the server-side session behind every JWT.

The document identifies and the code authenticates. Every answer about a document is the same
whether a customer has it or not: a code is issued, attempts are counted and documents are
locked for invented documents too, and a right code for a document with no customer fails like
a wrong one. Only a valid code for an existing customer tells them apart, and that already
authenticates.

A JWT alone cannot expire after inactivity nor be revoked, so each token names a row of
`sessions`. Times here are the real clock, never the simulated one (design 5.1).

Each function commits its own transaction before it raises: a failed attempt, a lockout or an
expired session must stay recorded even though the request fails.
"""

import hashlib
import hmac
import math
import secrets
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, cast

import jwt
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.adapters.db.audit import write_audit
from app.adapters.db.models import AuthChallenge, Case, SessionRecord
from app.adapters.db.session import Database, bind_context
from app.core.config import Settings
from app.core.errors import AppError
from app.core.logging import get_logger
from app.domain.pii import document_hash
from app.services.tools import settle_pending_action

log = get_logger("auth")

Role = Literal["customer", "analyst"]

# Statuses in which a case still waits on the customer. When the session ends, such a case is
# expired and its pending action dropped, so a "sí" after logging in again runs nothing.
WAITING_STATUSES = frozenset({"open", "identifying", "recognizing", "awaiting_confirmation"})

MIN_SECRET_LENGTH = 32


@dataclass(frozen=True)
class Principal:
    """Who is calling, as read from a valid session.

    Attributes:
        subject: Customer id for a customer, user name for an analyst.
        role: customer or analyst.
        session_id: The jti of the token, the key of its row in sessions.
    """

    subject: str
    role: Role
    session_id: str


@dataclass(frozen=True)
class IssuedToken:
    """A new session.

    Attributes:
        token: The signed JWT.
        role: Role it carries.
        expires_in_seconds: Time until its absolute expiry.
    """

    token: str
    role: Role
    expires_in_seconds: int


def check_secrets(settings: Settings) -> None:
    """Refuses to serve without the two keys a login needs.

    Args:
        settings: Application settings.

    Raises:
        ValueError: If JWT_SECRET is shorter than 32 characters, or DOCUMENT_HASH_KEY is empty,
            which would make every login fail (`make init` generates both).
    """
    if len(settings.jwt_secret) < MIN_SECRET_LENGTH:
        raise ValueError(
            f"JWT_SECRET must have at least {MIN_SECRET_LENGTH} characters; run make init"
        )
    if not settings.document_hash_key:
        raise ValueError("DOCUMENT_HASH_KEY is not set; login hashes the document with it")


LimitScope = Literal["otp_request", "otp_verify", "analyst_login", "chat"]

# One statement counts the request and restarts a window that ran out, so concurrent requests
# and several replicas share one count without a lock held across statements.
_COUNT_ADDRESS = text(
    """
    INSERT INTO auth_ip_limits AS l (scope, ip_key, window_start, request_count)
    VALUES (:scope, :key, :now, 1)
    ON CONFLICT (scope, ip_key) DO UPDATE SET
        window_start = CASE WHEN l.window_start <= :expired THEN :now ELSE l.window_start END,
        request_count = CASE WHEN l.window_start <= :expired THEN 1 ELSE l.request_count + 1 END
    RETURNING request_count, window_start
    """
)


def limit_address(
    db: Database, settings: Settings, scope: LimitScope, address: str, now: datetime
) -> None:
    """Counts a request from a client address and refuses it past the limit (TRZ-40).

    The limit per document does not stop one address from trying many documents, so each login
    endpoint also allows IP_REQUEST_LIMIT requests per address in each window. It runs before
    the document is read, so the answer is the same whether a customer has the document.
    Customer turns (`chat`) have CHAT_IP_REQUEST_LIMIT in the same window, so one address
    cannot spend the LLM without bound. The address is stored only as a keyed hash, and
    refused requests count too.

    Args:
        db: Database.
        settings: Application settings.
        scope: The endpoint, each with its own count.
        address: Client address, as read by the API layer.
        now: Real current time, naive UTC.

    Raises:
        AppError: ip_requests_limited (429) past the limit of the window, with Retry-After
            set to the seconds left of it.
    """
    key = hmac.new(
        settings.document_hash_key.encode(), f"address:{address}".encode(), hashlib.sha256
    ).hexdigest()
    expired = now - timedelta(minutes=settings.ip_request_window_minutes)
    with _transaction(db) as s:
        count, window_start = s.execute(
            _COUNT_ADDRESS, {"scope": scope, "key": key, "now": now, "expired": expired}
        ).one()
    limit = settings.chat_ip_request_limit if scope == "chat" else settings.ip_request_limit
    if count > limit:
        # Seconds left of the window, so the screen can say how long to wait.
        left = window_start - expired
        raise AppError(
            "ip_requests_limited",
            "Too many requests from this network. Try again later.",
            429,
            headers={"Retry-After": str(max(1, math.ceil(left.total_seconds())))},
        )


def request_code(
    db: Database, settings: Settings, document_type: str, document_number: str, now: datetime
) -> None:
    """Issues a one-time code for a document, whether or not a customer has it (CA1).

    With demo mode the code is the fixed demo code; otherwise it is random and only the local
    development log shows it (CA3). A locked document gets no new code, and the caller cannot
    tell. Failed attempts are not reset by a new code, so asking again does not buy attempts.
    More than OTP_REQUEST_LIMIT requests for one document in a window are refused, the same way
    for every document.

    Args:
        db: Database.
        settings: Application settings.
        document_type: Document type as typed by the customer.
        document_number: Document number as typed by the customer.
        now: Real current time, naive UTC.

    Raises:
        AppError: code_requests_limited (429) past the limit of the window.
    """
    key = document_hash(settings.document_hash_key, document_type, document_number)
    code = settings.demo_otp_code if settings.demo_mode else f"{secrets.randbelow(10**6):06d}"
    window = timedelta(minutes=settings.otp_request_window_minutes)
    with _transaction(db) as s:
        challenge = _challenge(s, key)
        if challenge.requests_since is None or now - challenge.requests_since >= window:
            challenge.requests_since, challenge.request_count = now, 0
        challenge.request_count += 1
        limited = challenge.request_count > settings.otp_request_limit
        issued = not limited and not _locked(challenge, now)
        if issued:
            challenge.code_hash = _code_hash(settings, key, code)
            challenge.expires_at = now + timedelta(minutes=settings.otp_ttl_minutes)
        challenge.updated_at = now
    if limited:
        raise AppError(
            "code_requests_limited", "Too many code requests. Try again in a few minutes.", 429
        )
    if issued and not settings.demo_mode and settings.app_env == "local":
        log.info("otp_code_issued", document_ref=key[:12], code=code)


def verify_code(
    db: Database,
    settings: Settings,
    document_type: str,
    document_number: str,
    code: str,
    now: datetime,
) -> IssuedToken:
    """Checks a one-time code and opens a customer session (CA2).

    A code is valid once. The third failed code in a row locks the document for
    OTP_LOCK_MINUTES and writes an audit event (CA4).

    Args:
        db: Database.
        settings: Application settings.
        document_type: Document type.
        document_number: Document number.
        code: Code typed by the customer.
        now: Real current time, naive UTC.

    Returns:
        The new session token.

    Raises:
        AppError: invalid_code (401) for a wrong, expired or used code, or a document with no
            customer; document_locked (429) while the document is locked.
    """
    key = document_hash(settings.document_hash_key, document_type, document_number)
    with _transaction(db) as s:
        challenge = _challenge(s, key)
        if _locked(challenge, now):
            failure = _locked_error()
        else:
            customer_id = s.execute(
                text("SELECT auth_customer_id(:key)"), {"key": key}
            ).scalar_one()
            valid = (
                challenge.code_hash is not None
                and challenge.expires_at is not None
                and challenge.expires_at > now
                and hmac.compare_digest(challenge.code_hash, _code_hash(settings, key, code))
            )
            if valid and customer_id is not None:
                challenge.code_hash, challenge.expires_at = None, None
                challenge.failed_attempts, challenge.locked_until = 0, None
                challenge.updated_at = now
                end_stale_sessions(s, settings, customer_id, now)
                return _open_session(s, settings, customer_id, "customer", now)
            failure = _count_failure(s, settings, challenge, customer_id, now)
    raise failure


def analyst_login(
    db: Database, settings: Settings, username: str, password: str, now: datetime
) -> IssuedToken:
    """Opens an analyst session with the test credentials of the environment (CA7).

    Args:
        db: Database.
        settings: Application settings.
        username: User name.
        password: Password.
        now: Real current time, naive UTC.

    Returns:
        The new session token, with role analyst.

    Raises:
        AppError: invalid_credentials (401) when they do not match, or no password is set.
    """
    # Both comparisons always run, so the time taken does not say which one failed.
    user_ok = hmac.compare_digest(username.encode(), settings.analyst_demo_user.encode())
    password_ok = hmac.compare_digest(password.encode(), settings.analyst_demo_password.encode())
    if not (user_ok and password_ok and settings.analyst_demo_password):
        raise AppError("invalid_credentials", "User or password is not valid.", 401)
    with _transaction(db) as s:
        return _open_session(s, settings, username, "analyst", now)


def authenticate(db: Database, settings: Settings, token: str, now: datetime) -> Principal:
    """Reads a bearer token and its session, and keeps the session alive.

    A session idle for more than SESSION_IDLE_MINUTES, or older than SESSION_MAX_MINUTES, ends
    here: the call gets session_expired and the waiting case of the session is expired (CA5).

    Args:
        db: Database.
        settings: Application settings.
        token: The JWT of the Authorization header.
        now: Real current time, naive UTC.

    Returns:
        The caller.

    Raises:
        AppError: invalid_token, session_expired or session_revoked, all 401.
    """
    try:
        # Expiry is checked against `now` below, so tests can move the clock.
        claims = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            options={"verify_exp": False, "require": ["sub", "role", "jti", "iat", "exp"]},
        )
    except jwt.InvalidTokenError:
        raise _invalid_token() from None
    with _transaction(db) as s:
        rec = s.get(SessionRecord, str(claims["jti"]), with_for_update=True)
        if rec is None or rec.subject != claims["sub"] or rec.role != claims["role"]:
            failure = _invalid_token()
        elif rec.ended_reason == "logout":
            failure = AppError("session_revoked", "The session was closed.", 401)
        elif rec.ended_at is not None:
            failure = _expired()
        elif now >= rec.expires_at or now >= _as_utc(claims["exp"]):
            end_session(s, rec, now, "max_age")
            failure = _expired()
        elif now - rec.last_seen_at > timedelta(minutes=settings.session_idle_minutes):
            end_session(s, rec, now, "idle")
            failure = _expired()
        else:
            rec.last_seen_at = now
            return Principal(subject=rec.subject, role=cast(Role, rec.role), session_id=rec.id)
    raise failure


def logout(db: Database, principal: Principal, now: datetime) -> None:
    """Revokes the caller's session; its token stops working at once.

    Args:
        db: Database.
        principal: The caller.
        now: Real current time, naive UTC.
    """
    with _transaction(db) as s:
        rec = s.get(SessionRecord, principal.session_id, with_for_update=True)
        if rec is not None and rec.ended_at is None:
            end_session(s, rec, now, "logout")


def remember_case(session: Session, principal: Principal, case_id: str) -> None:
    """Records the case a customer session is working on, to expire it with the session.

    Args:
        session: Request session.
        principal: The customer.
        case_id: Case of the turn.
    """
    rec = session.get(SessionRecord, principal.session_id)
    if rec is not None:
        rec.case_id = case_id


def end_stale_sessions(
    session: Session, settings: Settings, customer_id: str, now: datetime
) -> None:
    """Ends the customer's sessions that expired without being used again.

    Runs at login, so a case left waiting by a session that simply went quiet is expired before
    the new session can confirm anything on it.

    Args:
        session: Open session.
        settings: Application settings.
        customer_id: Customer logging in.
        now: Real current time, naive UTC.
    """
    idle_since = now - timedelta(minutes=settings.session_idle_minutes)
    stale = session.scalars(
        select(SessionRecord)
        .where(
            SessionRecord.subject == customer_id,
            SessionRecord.role == "customer",
            SessionRecord.ended_at.is_(None),
            (SessionRecord.last_seen_at < idle_since) | (SessionRecord.expires_at <= now),
        )
        .with_for_update()
    ).all()
    for rec in stale:
        end_session(session, rec, now, "max_age" if rec.expires_at <= now else "idle")


def end_session(
    session: Session,
    rec: SessionRecord,
    now: datetime,
    reason: Literal["idle", "max_age", "logout"],
) -> None:
    """Ends a session and expires the case it left waiting, dropping its pending action.

    Args:
        session: Open session.
        rec: The session row.
        now: Real current time, naive UTC.
        reason: Why it ends.
    """
    rec.ended_at, rec.ended_reason = now, reason
    if rec.role != "customer" or rec.case_id is None:
        return
    bind_context(session, customer_id=rec.subject)
    case = session.get(Case, rec.case_id)
    if case is None or case.status not in WAITING_STATUSES:
        return
    before, pending = case.status, case.recommended_action
    case.status, case.recommended_action = "expired", None
    dropped = settle_pending_action(session, case.id, "canceled")
    write_audit(
        session,
        "auth",
        "case_expired",
        case.id,
        {"session_end": reason},
        {"status_before": before, "pending_action_dropped": pending, "action_id": dropped},
    )


# ---- helpers ----------------------------------------------------------------------------


@contextmanager
def _transaction(db: Database) -> Iterator[Session]:
    """One committed transaction; a database failure becomes the 503 of the error contract."""
    try:
        with db.session() as s:
            yield s
    except SQLAlchemyError as exc:
        raise AppError("db_unavailable", "Database is not reachable.", 503) from exc


def _challenge(session: Session, key: str) -> AuthChallenge:
    """The challenge row of a document, created if missing and locked for this transaction."""
    session.execute(insert(AuthChallenge).values(document_key=key).on_conflict_do_nothing())
    return session.scalars(
        select(AuthChallenge).where(AuthChallenge.document_key == key).with_for_update()
    ).one()


def _locked(challenge: AuthChallenge, now: datetime) -> bool:
    return challenge.locked_until is not None and challenge.locked_until > now


def _count_failure(
    session: Session,
    settings: Settings,
    challenge: AuthChallenge,
    customer_id: str | None,
    now: datetime,
) -> AppError:
    challenge.failed_attempts += 1
    challenge.updated_at = now
    if challenge.failed_attempts < settings.otp_max_attempts:
        return AppError("invalid_code", "The code is not valid for this document.", 401)
    challenge.failed_attempts = 0
    challenge.code_hash, challenge.expires_at = None, None
    challenge.locked_until = now + timedelta(minutes=settings.otp_lock_minutes)
    # A document with no customer is filed with no customer, under the authentication role.
    if customer_id is None:
        bind_context(session, role="auth")
    else:
        bind_context(session, customer_id=customer_id)
    write_audit(
        session,
        "auth",
        "document_locked",
        None,
        {"document_ref": challenge.document_key[:12]},
        {
            "failed_attempts": settings.otp_max_attempts,
            "locked_until": challenge.locked_until.isoformat(),
        },
    )
    log.warning("document_locked", document_ref=challenge.document_key[:12])
    return _locked_error()


def _code_hash(settings: Settings, key: str, code: str) -> str:
    return hmac.new(
        settings.jwt_secret.encode(), f"{key}:{code.strip()}".encode(), hashlib.sha256
    ).hexdigest()


def _open_session(
    session: Session, settings: Settings, subject: str, role: Role, now: datetime
) -> IssuedToken:
    jti = uuid.uuid4().hex
    expires_at = now + timedelta(minutes=settings.session_max_minutes)
    session.add(
        SessionRecord(
            id=jti,
            subject=subject,
            role=role,
            created_at=now,
            last_seen_at=now,
            expires_at=expires_at,
        )
    )
    claims = {
        "sub": subject,
        "role": role,
        "jti": jti,
        "iat": int(now.replace(tzinfo=UTC).timestamp()),
        "exp": int(expires_at.replace(tzinfo=UTC).timestamp()),
    }
    token = jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return IssuedToken(token=token, role=role, expires_in_seconds=settings.session_max_minutes * 60)


def _as_utc(epoch: int) -> datetime:
    return datetime.fromtimestamp(epoch, UTC).replace(tzinfo=None)


def _expired() -> AppError:
    return AppError(
        "session_expired", "The session expired. Log in again to continue your case.", 401
    )


def _locked_error() -> AppError:
    return AppError("document_locked", "Too many failed codes. Try again in a few minutes.", 429)


def _invalid_token() -> AppError:
    return AppError("invalid_token", "The session token is not valid.", 401)
