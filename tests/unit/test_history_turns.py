"""Steps of a case grouped by turn (TRZ-34 CA4, follow-up): numbered in audit order, each turn
headed by what the customer did, with time relative to the start of its turn and durations."""

from datetime import datetime, timedelta

from app.domain.history import group_turns

T0 = datetime(2026, 10, 4, 15, 0, 0)


def _row(
    i: int,
    trace: str,
    actor: str,
    action: str,
    ms: int = 0,
    latency: int | None = None,
    result: dict | None = None,
) -> dict:
    return {
        "id": i,
        "trace_id": trace,
        "actor": actor,
        "action": action,
        "payload": {},
        "result": result or {},
        "latency_ms": latency,
        "created_at": T0 + timedelta(milliseconds=ms),
    }


ROWS = [
    _row(10, "t1", "agent", "comprehend", 0, latency=1400),
    _row(11, "t1", "tool", "identify_transaction", 1450),
    _row(12, "t1", "tool", "show_charge_detail", 1460),
    _row(13, "t1", "agent", "turn_complete", 1500, latency=1520),
    _row(14, "t2", "agent", "recognize", 0, result={"choice": "not_recognized"}),
    _row(15, "t2", "policy", "decide", 30),
    _row(16, "t2", "agent", "turn_complete", 40, latency=45),
    _row(17, "t3", "agent", "confirm", 0),
    _row(18, "t3", "tool", "register_dispute", 20),
    _row(19, "t3", "agent", "verify_action", 35),
]


def test_steps_are_grouped_by_request_and_numbered_in_audit_order() -> None:
    turns = group_turns(ROWS, "customer", "es")
    assert [t.number for t in turns] == [1, 2, 3]
    assert [[s.number for s in t.steps] for t in turns] == [[1, 2, 3, 4], [5, 6, 7], [8, 9, 10]]
    assert [s.row["id"] for t in turns for s in t.steps] == [r["id"] for r in ROWS]


def test_each_turn_is_headed_by_what_the_customer_did_in_both_languages() -> None:
    assert [t.kind for t in group_turns(ROWS, "customer", "es")] == [
        "message",
        "recognition",
        "confirmation",
    ]
    assert [t.header for t in group_turns(ROWS, "customer", "es")] == [
        "Escribiste un mensaje",
        "Dijiste que no reconoces el cargo",
        "Confirmaste la acción",
    ]
    assert [t.header for t in group_turns(ROWS, "customer", "pt")] == [
        "Você escreveu uma mensagem",
        "Você disse que não reconhece a cobrança",
        "Você confirmou a ação",
    ]
    assert [t.header for t in group_turns(ROWS, "analyst", "es")] == [
        "El cliente escribió un mensaje",
        "El cliente dijo que no reconoce el cargo",
        "El cliente confirmó la acción",
    ]


def test_steps_carry_time_from_the_start_of_their_turn_and_their_duration() -> None:
    first, second, _ = group_turns(ROWS, "customer", "es")
    assert [s.offset_ms for s in first.steps] == [0, 1450, 1460, 1500]
    assert [s.duration_ms for s in first.steps] == [1400, None, None, 1520]
    assert [s.offset_ms for s in second.steps] == [0, 30, 40]


def test_a_turn_written_before_rows_had_their_own_time_shows_no_offsets() -> None:
    same = [_row(1, "t1", "agent", "comprehend", 0, latency=900), _row(2, "t1", "tool", "x", 0)]
    [turn] = group_turns(same, "customer", "es")
    assert [s.offset_ms for s in turn.steps] == [None, None]
    assert [s.duration_ms for s in turn.steps] == [900, None]


def test_other_turns_are_named_by_who_acted() -> None:
    rows = [
        _row(1, "a", "agent", "choose", 0),
        _row(2, "b", "agent", "recognize", 0, result={"choice": "recognized"}),
        _row(3, "c", "agent", "decline", 0),
        _row(4, "d", "agent", "button_press", 0),
        _row(5, "e", "customer", "info_reply", 0),
        _row(6, "f", "agent", "customer_note", 0),
        _row(7, "g", "human", "decision", 0),
        _row(8, "h", "system", "notify", 0),
        _row(9, "i", "auth", "case_expired", 0),
        _row(10, "j", "system", "mark_simulated", 0),
    ]
    assert [t.header for t in group_turns(rows, "customer", "es")] == [
        "Elegiste una opción",
        "Dijiste que reconoces el cargo",
        "No aceptaste la acción",
        "Pulsaste «No lo reconozco» en un movimiento",
        "Respondiste la pregunta de la analista",
        "Escribiste en el caso, que ya estaba con una persona",
        "La analista decidió",
        "El sistema",
        "La sesión expiró",
        "Estado del demo [simulado]",
    ]
    assert [t.header for t in group_turns(rows, "analyst", "pt")] == [
        "O cliente escolheu uma opção",
        "O cliente disse que reconhece a cobrança",
        "O cliente não aceitou a ação",
        "O cliente tocou em «Não reconheço» em uma movimentação",
        "O cliente respondeu à pergunta da analista",
        "O cliente escreveu no caso, que já estava com uma pessoa",
        "A analista decidiu",
        "O sistema",
        "A sessão expirou",
        "Estado da demonstração [simulado]",
    ]


def test_no_date_or_clock_time_leaves_the_grouping() -> None:
    for turn in group_turns(ROWS, "customer", "es"):
        assert "2026" not in turn.header
        for step in turn.steps:
            assert all(v is None or isinstance(v, int) for v in (step.offset_ms, step.duration_ms))


SEEDED = [
    # seed-demo writes every scripted turn of a case with one trace_id.
    _row(20, "seed", "agent", "button_press", 0),
    _row(21, "seed", "tool", "show_charge_detail", 10),
    _row(22, "seed", "agent", "turn_complete", 20, latency=25),
    _row(23, "seed", "agent", "recognize", 30, result={"choice": "not_recognized"}),
    _row(24, "seed", "policy", "decide", 40),
    _row(25, "seed", "agent", "turn_complete", 50, latency=22),
    _row(26, "seed", "system", "mark_simulated", 60),
]


def test_a_turn_ends_after_its_turn_complete_even_within_one_trace() -> None:
    turns = group_turns(SEEDED, "analyst", "es")
    assert [t.kind for t in turns] == ["button", "recognition", "demo"]
    assert [[s.row["id"] for s in t.steps] for t in turns] == [[20, 21, 22], [23, 24, 25], [26]]
    assert [[s.number for s in t.steps] for t in turns] == [[1, 2, 3], [4, 5, 6], [7]]
    assert [s.offset_ms for s in turns[1].steps] == [0, 10, 20]


def test_mark_simulated_is_a_block_of_its_own() -> None:
    rows = [
        _row(1, "seed", "agent", "comprehend", 0),
        _row(2, "seed", "system", "mark_simulated", 5),
        _row(3, "seed", "human", "decision", 9),
    ]
    turns = group_turns(rows, "customer", "es")
    assert [[s.row["id"] for s in t.steps] for t in turns] == [[1], [2], [3]]
    assert [t.header for t in turns] == [
        "Escribiste un mensaje",
        "Estado del demo [simulado]",
        "La analista decidió",
    ]
