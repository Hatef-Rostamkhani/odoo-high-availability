-- Run only as the PostgreSQL administrator in odoo_secrets_control.
-- APP_DATABASE is replaced with a strictly validated identifier by bootstrap.
BEGIN;
CREATE SCHEMA IF NOT EXISTS secret_control AUTHORIZATION postgres;
REVOKE ALL ON SCHEMA secret_control FROM PUBLIC;
CREATE TABLE IF NOT EXISTS secret_control.configuration (
    singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
    enabled boolean NOT NULL DEFAULT false,
    application_database text NOT NULL DEFAULT 'APP_DATABASE',
    application_namespace text NOT NULL DEFAULT 'APP_NAMESPACE',
    ttl_seconds integer NOT NULL DEFAULT 86400 CHECK(ttl_seconds BETWEEN 60 AND 86400),
    lead_seconds integer NOT NULL DEFAULT 900 CHECK(lead_seconds BETWEEN 10 AND 3600),
    active_role text,
    active_generation text
);
INSERT INTO secret_control.configuration(singleton) VALUES(true) ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS secret_control.generations (
    generation text PRIMARY KEY,
    role_name text NOT NULL CHECK(role_name IN ('odoo_login_a','odoo_login_b')),
    issued_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    retired_at timestamptz,
    CHECK(expires_at <= issued_at + interval '24 hours')
);
CREATE TABLE IF NOT EXISTS secret_control.operation (
    singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
    generation text NOT NULL REFERENCES secret_control.generations(generation),
    old_role text,
    new_role text NOT NULL,
    phase text NOT NULL CHECK(phase IN ('ISSUED','PUBLISHED','WEB_READY','CRON_READY')),
    started_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS secret_control.events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    generation text,
    event text NOT NULL,
    happened_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
ALTER TABLE secret_control.operation ADD COLUMN IF NOT EXISTS bao_version bigint;
CREATE TABLE IF NOT EXISTS secret_control.maintenance (
    singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
    owner text NOT NULL,
    expires_at timestamptz NOT NULL
);
DO $$
BEGIN
    IF NOT EXISTS(SELECT FROM pg_roles WHERE rolname='odoo_login_a') THEN
        CREATE ROLE odoo_login_a NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 0;
    END IF;
    IF NOT EXISTS(SELECT FROM pg_roles WHERE rolname='odoo_login_b') THEN
        CREATE ROLE odoo_login_b NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 0;
    END IF;
END $$;
GRANT odoo TO odoo_login_a, odoo_login_b WITH INHERIT TRUE, SET TRUE, ADMIN FALSE;
ALTER ROLE odoo_login_a IN DATABASE APP_DATABASE SET role = 'odoo';
ALTER ROLE odoo_login_b IN DATABASE APP_DATABASE SET role = 'odoo';

CREATE OR REPLACE FUNCTION secret_control.status() RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,secret_control AS $$
SELECT jsonb_build_object(
    'clock',clock_timestamp(),'configuration',to_jsonb(c),
    'operation',(SELECT to_jsonb(o) FROM secret_control.operation o),
    'completed_at',(SELECT max(happened_at) FROM secret_control.events WHERE event='RETIRED_PREVIOUS'),
    'maintenance',(SELECT to_jsonb(m) FROM secret_control.maintenance m WHERE expires_at>clock_timestamp()),
    'generations',COALESCE((SELECT jsonb_agg(to_jsonb(g)) FROM secret_control.generations g), '[]'::jsonb),
    'connections',COALESCE((SELECT jsonb_object_agg(role_name,n) FROM
        (SELECT usename AS role_name,count(*) AS n FROM pg_stat_activity
         WHERE usename IN ('odoo_login_a','odoo_login_b') GROUP BY usename) x),'{}'::jsonb))
FROM secret_control.configuration c WHERE singleton;
$$;

CREATE OR REPLACE FUNCTION secret_control.set_budget(a integer,b integer) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,secret_control AS $$
BEGIN
    IF a<0 OR b<0 OR a+b<>45 THEN RAISE EXCEPTION 'Invalid aggregate application budget'; END IF;
    PERFORM pg_advisory_xact_lock(789306491214);
    IF (SELECT count(*) FROM pg_stat_activity WHERE usename='odoo_login_a')>a
       OR (SELECT count(*) FROM pg_stat_activity WHERE usename='odoo_login_b')>b THEN RETURN false; END IF;
    EXECUTE format('ALTER ROLE odoo_login_a CONNECTION LIMIT %s',a);
    EXECUTE format('ALTER ROLE odoo_login_b CONNECTION LIMIT %s',b);
    RETURN true;
END $$;

DROP FUNCTION IF EXISTS secret_control.issue(text,text,text);
CREATE OR REPLACE FUNCTION secret_control.issue(target text,gen text,scram text,maint_owner text DEFAULT NULL) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,secret_control AS $$
DECLARE c secret_control.configuration%ROWTYPE; issued timestamptz; expiry timestamptz; previous text;
BEGIN
    IF target NOT IN ('odoo_login_a','odoo_login_b') OR gen !~ '^[a-f0-9]{32}$'
       OR scram !~ '^SCRAM-SHA-256\$4096:' THEN RAISE EXCEPTION 'Invalid credential request'; END IF;
    IF NOT pg_try_advisory_xact_lock(789306491213) AND NOT EXISTS(
        SELECT FROM secret_control.maintenance WHERE owner=maint_owner AND expires_at>clock_timestamp())
    THEN RAISE EXCEPTION 'Maintenance is active'; END IF;
    PERFORM pg_advisory_xact_lock(789306491214);
    SELECT * INTO c FROM secret_control.configuration WHERE singleton FOR UPDATE;
    IF EXISTS(SELECT FROM secret_control.operation) THEN
        IF EXISTS(SELECT FROM secret_control.operation WHERE generation=gen AND new_role=target) THEN
            RETURN (SELECT to_jsonb(g) FROM secret_control.generations g WHERE generation=gen);
        END IF;
        RAISE EXCEPTION 'Another generation is in progress';
    END IF;
    IF target=c.active_role THEN RAISE EXCEPTION 'Cannot replace the current login credential'; END IF;
    IF EXISTS(SELECT FROM pg_stat_activity WHERE usename=target) THEN RAISE EXCEPTION 'Inactive role still has sessions'; END IF;
    IF EXISTS(SELECT FROM secret_control.generations WHERE generation=gen) THEN RAISE EXCEPTION 'A generation cannot be reused'; END IF;
    previous=c.active_role;
    IF previous IS NULL THEN
        IF NOT secret_control.set_budget(CASE WHEN target='odoo_login_a' THEN 45 ELSE 0 END,
                                         CASE WHEN target='odoo_login_b' THEN 45 ELSE 0 END)
        THEN RAISE EXCEPTION 'Application budget is busy'; END IF;
    ELSIF NOT secret_control.set_budget(CASE WHEN target='odoo_login_a' THEN 11 ELSE 34 END,
                                       CASE WHEN target='odoo_login_b' THEN 11 ELSE 34 END)
    THEN RAISE EXCEPTION 'Application budget is busy'; END IF;
    issued=clock_timestamp(); expiry=issued+make_interval(secs=>c.ttl_seconds);
    EXECUTE format('ALTER ROLE %I LOGIN PASSWORD %L VALID UNTIL %L',target,scram,expiry);
    INSERT INTO secret_control.generations VALUES(gen,target,issued,expiry,NULL);
    INSERT INTO secret_control.operation(singleton,generation,old_role,new_role,phase)
        VALUES(true,gen,previous,target,'ISSUED');
    INSERT INTO secret_control.events(generation,event) VALUES(gen,'ISSUED');
    RETURN (SELECT to_jsonb(g) FROM secret_control.generations g WHERE generation=gen);
END $$;

CREATE OR REPLACE FUNCTION secret_control.advance(gen text,next_phase text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,secret_control AS $$
DECLARE current_phase text;
BEGIN
    SELECT phase INTO current_phase FROM secret_control.operation WHERE generation=gen FOR UPDATE;
    IF current_phase IS NULL THEN RAISE EXCEPTION 'Unknown in-progress generation'; END IF;
    IF current_phase=next_phase THEN RETURN; END IF;
    IF NOT ((current_phase='ISSUED' AND next_phase='PUBLISHED') OR
            (current_phase='PUBLISHED' AND next_phase='WEB_READY') OR
            (current_phase='WEB_READY' AND next_phase='CRON_READY')) THEN
        RAISE EXCEPTION 'Invalid rotation transition';
    END IF;
    UPDATE secret_control.operation SET phase=next_phase WHERE generation=gen;
    INSERT INTO secret_control.events(generation,event) VALUES(gen,next_phase);
END $$;

CREATE OR REPLACE FUNCTION secret_control.publication(gen text,version bigint) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,secret_control AS $$
BEGIN
    IF version<1 OR NOT EXISTS(SELECT FROM secret_control.operation WHERE generation=gen)
    THEN RAISE EXCEPTION 'Invalid credential publication'; END IF;
    UPDATE secret_control.operation SET bao_version=version WHERE generation=gen;
END $$;

-- This function intentionally has no writes: strict synchronous replication
-- cannot prevent it from terminating already-expired sessions.
CREATE OR REPLACE FUNCTION secret_control.expire_sessions() RETURNS integer
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,secret_control AS $$
DECLARE p record; terminated integer=0;
BEGIN
    FOR p IN
        SELECT a.pid,a.backend_start,a.usename,g.generation,g.expires_at FROM pg_stat_activity a
        JOIN (SELECT DISTINCT ON(role_name) role_name,generation,issued_at,expires_at
              FROM secret_control.generations ORDER BY role_name,issued_at DESC) g
          ON a.usename=g.role_name
        -- A TLS/authentication handshake can start before issuance and finish
        -- after the new password becomes usable. Do not exempt that backend.
        WHERE g.expires_at<=clock_timestamp() AND a.backend_start<g.expires_at
          AND a.pid<>pg_backend_pid()
    LOOP
        PERFORM pg_stat_clear_snapshot();
        IF EXISTS(SELECT FROM pg_stat_activity WHERE pid=p.pid AND backend_start=p.backend_start AND usename=p.usename)
           AND p.generation=(SELECT generation FROM secret_control.generations WHERE role_name=p.usename ORDER BY issued_at DESC LIMIT 1)
           AND pg_terminate_backend(p.pid) THEN terminated=terminated+1; END IF;
    END LOOP;
    RETURN terminated;
END $$;

CREATE OR REPLACE FUNCTION secret_control.finish(gen text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,secret_control AS $$
DECLARE o secret_control.operation%ROWTYPE; expired boolean;
BEGIN
    PERFORM pg_advisory_xact_lock(789306491214);
    SELECT * INTO o FROM secret_control.operation WHERE generation=gen FOR UPDATE;
    IF o.generation IS NULL THEN
        IF EXISTS(SELECT FROM secret_control.configuration WHERE active_generation=gen) THEN RETURN; END IF;
        RAISE EXCEPTION 'Unknown generation';
    END IF;
    IF o.phase<>'CRON_READY' THEN RAISE EXCEPTION 'Web and cron must be validated first'; END IF;
    SELECT expires_at<=clock_timestamp() INTO expired FROM secret_control.generations WHERE generation=gen;
    IF expired THEN RAISE EXCEPTION 'Cannot activate an expired generation'; END IF;
    IF o.old_role IS NOT NULL THEN
        EXECUTE format('ALTER ROLE %I NOLOGIN PASSWORD NULL CONNECTION LIMIT 0',o.old_role);
        PERFORM pg_terminate_backend(pid,500) FROM pg_stat_activity WHERE usename=o.old_role;
        UPDATE secret_control.generations SET retired_at=clock_timestamp()
            WHERE role_name=o.old_role AND retired_at IS NULL;
    END IF;
    EXECUTE format('ALTER ROLE %I CONNECTION LIMIT 45',o.new_role);
    UPDATE secret_control.configuration SET active_role=o.new_role,active_generation=gen WHERE singleton;
    INSERT INTO secret_control.events(generation,event) VALUES(gen,'RETIRED_PREVIOUS');
    DELETE FROM secret_control.operation WHERE generation=gen;
END $$;

CREATE OR REPLACE FUNCTION secret_control.revoke(gen text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,secret_control AS $$
DECLARE target text;
BEGIN
    PERFORM pg_advisory_xact_lock(789306491214);
    SELECT role_name INTO target FROM secret_control.generations WHERE generation=gen;
    IF target IS NULL THEN RAISE EXCEPTION 'Unknown generation'; END IF;
    IF EXISTS(SELECT FROM secret_control.generations
              WHERE role_name=target AND issued_at>(SELECT issued_at FROM secret_control.generations WHERE generation=gen))
    THEN RAISE EXCEPTION 'Cannot revoke a superseded generation by role name'; END IF;
    PERFORM pg_terminate_backend(pid,500) FROM pg_stat_activity WHERE usename=target;
    EXECUTE format('ALTER ROLE %I NOLOGIN PASSWORD NULL CONNECTION LIMIT 0',target);
    UPDATE secret_control.generations SET expires_at=LEAST(expires_at,clock_timestamp()),retired_at=clock_timestamp()
        WHERE generation=gen;
    INSERT INTO secret_control.events(generation,event) VALUES(gen,'REVOKED');
END $$;

CREATE OR REPLACE FUNCTION secret_control.enable(gen text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,secret_control AS $$
BEGIN
    IF NOT EXISTS(SELECT FROM secret_control.configuration WHERE active_generation=gen)
       OR NOT EXISTS(SELECT FROM secret_control.generations WHERE generation=gen AND expires_at>clock_timestamp())
    THEN RAISE EXCEPTION 'A validated current generation is required'; END IF;
    UPDATE secret_control.configuration SET enabled=true WHERE singleton;
END $$;

CREATE OR REPLACE FUNCTION secret_control.abort(gen text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,secret_control AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(789306491214);
    IF NOT EXISTS(SELECT FROM secret_control.operation WHERE generation=gen) THEN RETURN; END IF;
    PERFORM secret_control.revoke(gen);
    DELETE FROM secret_control.operation WHERE generation=gen;
    INSERT INTO secret_control.events(generation,event) VALUES(gen,'ABORTED');
END $$;

CREATE OR REPLACE FUNCTION secret_control.retire_expired() RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,secret_control AS $$
DECLARE g record; retired integer=0;
BEGIN
    FOR g IN SELECT * FROM (
        SELECT DISTINCT ON(role_name) * FROM secret_control.generations ORDER BY role_name,issued_at DESC
    ) latest WHERE expires_at<=clock_timestamp() AND retired_at IS NULL LOOP
        PERFORM secret_control.revoke(g.generation);
        retired=retired+1;
    END LOOP;
    RETURN retired;
END $$;

CREATE OR REPLACE FUNCTION secret_control.maintenance_begin(owner_id text,budget integer) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,secret_control AS $$
DECLARE current_gen text;
BEGIN
    IF owner_id !~ '^[a-f0-9]{32}$' OR budget<30 OR budget>3600 THEN RAISE EXCEPTION 'Invalid maintenance budget'; END IF;
    IF NOT pg_try_advisory_xact_lock(789306491213) THEN RAISE EXCEPTION 'Rotation or maintenance is active'; END IF;
    IF EXISTS(SELECT FROM secret_control.maintenance WHERE expires_at>clock_timestamp() AND owner<>owner_id)
    THEN RAISE EXCEPTION 'Another maintenance lease is active'; END IF;
    IF EXISTS(SELECT FROM secret_control.operation) THEN RAISE EXCEPTION 'Finish credential transition before maintenance'; END IF;
    SELECT active_generation INTO current_gen FROM secret_control.configuration WHERE singleton;
    IF current_gen IS NOT NULL AND NOT EXISTS(SELECT FROM secret_control.generations WHERE generation=current_gen
        AND expires_at>clock_timestamp()+make_interval(secs=>budget+60))
    THEN RAISE EXCEPTION 'Insufficient credential lifetime for maintenance'; END IF;
    INSERT INTO secret_control.maintenance VALUES(true,owner_id,clock_timestamp()+make_interval(secs=>budget))
        ON CONFLICT(singleton) DO UPDATE SET owner=EXCLUDED.owner,expires_at=EXCLUDED.expires_at;
END $$;

CREATE OR REPLACE FUNCTION secret_control.maintenance_end(owner_id text) RETURNS void
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,secret_control AS $$
DELETE FROM secret_control.maintenance WHERE owner=owner_id;
$$;

REVOKE ALL ON ALL TABLES IN SCHEMA secret_control FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA secret_control FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA secret_control FROM PUBLIC;
GRANT USAGE ON SCHEMA secret_control TO odoo_secret_rotator,odoo_secret_guard;
GRANT EXECUTE ON FUNCTION secret_control.status() TO odoo_secret_rotator,odoo_secret_guard;
GRANT EXECUTE ON FUNCTION secret_control.issue(text,text,text,text),secret_control.set_budget(integer,integer),
    secret_control.advance(text,text),secret_control.publication(text,bigint),secret_control.finish(text),secret_control.revoke(text),secret_control.enable(text),
    secret_control.abort(text),secret_control.retire_expired(),
    secret_control.maintenance_begin(text,integer),secret_control.maintenance_end(text)
    TO odoo_secret_rotator;
GRANT EXECUTE ON FUNCTION secret_control.expire_sessions() TO odoo_secret_guard;
COMMIT;
