-- Requests to each login endpoint, counted per client address in a fixed window (TRZ-40). The
-- limit per document of 0008 does not stop one address asking codes for many documents. The
-- address is stored only as a keyed hash. No row level security: like auth_challenges, it is
-- read before the service knows the customer and holds no customer data.
CREATE TABLE auth_ip_limits (
    scope         text NOT NULL,
    ip_key        text NOT NULL,
    window_start  timestamp NOT NULL,
    request_count integer NOT NULL,
    PRIMARY KEY (scope, ip_key)
);

GRANT SELECT, INSERT, UPDATE ON auth_ip_limits TO trazo_app;
