-- Audio clips join photos as private R2 objects. SQLite cannot alter a CHECK
-- constraint, so events is rebuilt. DROP TABLE runs no DELETE triggers, so the
-- latest, usage and daily tables keep their rows; the triggers are recreated below.
-- Raised attention takes a photo a minute (up to 1440 a day), hence the new daily
-- limits; photos and audio share the 6 GB object limit.
CREATE TABLE events_new (
 device TEXT NOT NULL, id TEXT NOT NULL, observed REAL NOT NULL,
 received REAL NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('measurement','photo','audio')),
 source TEXT NOT NULL, payload TEXT NOT NULL, fingerprint TEXT NOT NULL,
 object_key TEXT, bytes INTEGER NOT NULL DEFAULT 0,
 state TEXT NOT NULL CHECK(state IN ('pending','ready','deleting')),
 PRIMARY KEY(device,id)
);
-- statement-breakpoint
INSERT INTO events_new SELECT device,id,observed,received,kind,source,payload,fingerprint,object_key,bytes,state FROM events;
-- statement-breakpoint
DROP TABLE events;
-- statement-breakpoint
ALTER TABLE events_new RENAME TO events;
-- statement-breakpoint
CREATE INDEX timeline ON events(device,kind,state,observed,id);
-- statement-breakpoint
CREATE INDEX by_source ON events(device,source,state,observed);
-- statement-breakpoint
CREATE INDEX retention ON events(kind,observed);
-- statement-breakpoint
CREATE INDEX pending_age ON events(state,received);
-- statement-breakpoint
CREATE INDEX object_lookup ON events(object_key);
-- statement-breakpoint
CREATE TRIGGER latest_insert AFTER INSERT ON events WHEN NEW.state='ready' BEGIN
 INSERT INTO latest VALUES(NEW.device,NEW.kind,NEW.source,NEW.id,NEW.observed)
 ON CONFLICT(device,kind,source) DO UPDATE SET id=excluded.id,observed=excluded.observed
 WHERE (excluded.observed,excluded.id)>(latest.observed,latest.id);
END;
-- statement-breakpoint
CREATE TRIGGER latest_ready AFTER UPDATE OF state ON events WHEN NEW.state='ready' BEGIN
 INSERT INTO latest VALUES(NEW.device,NEW.kind,NEW.source,NEW.id,NEW.observed)
 ON CONFLICT(device,kind,source) DO UPDATE SET id=excluded.id,observed=excluded.observed
 WHERE (excluded.observed,excluded.id)>(latest.observed,latest.id);
END;
-- statement-breakpoint
CREATE TRIGGER latest_delete AFTER DELETE ON events BEGIN
 DELETE FROM latest WHERE device=OLD.device AND kind=OLD.kind AND source=OLD.source AND id=OLD.id;
END;
-- statement-breakpoint
CREATE TRIGGER check_quota BEFORE INSERT ON events
WHEN NOT EXISTS (SELECT 1 FROM events WHERE device=NEW.device AND id=NEW.id)
BEGIN
 SELECT (CASE WHEN NEW.kind IN ('photo','audio') AND (SELECT photo_bytes FROM usage WHERE id=1)+NEW.bytes>6000000000
   THEN RAISE(ABORT,'monitor_quota') END);
 SELECT (CASE WHEN COALESCE((SELECT count FROM daily WHERE day=CAST(NEW.received/86400 AS INTEGER) AND kind=NEW.kind),0)
   >= CASE WHEN NEW.kind='measurement' THEN 4096 ELSE 1600 END
   THEN RAISE(ABORT,'monitor_quota') END);
END;
-- statement-breakpoint
CREATE TRIGGER event_usage AFTER INSERT ON events BEGIN
 UPDATE usage SET photo_bytes=photo_bytes+NEW.bytes WHERE id=1;
 INSERT INTO daily VALUES(CAST(NEW.received/86400 AS INTEGER),NEW.kind,1)
 ON CONFLICT(day,kind) DO UPDATE SET count=count+1;
END;
-- statement-breakpoint
CREATE TRIGGER release_usage AFTER DELETE ON events BEGIN
 UPDATE usage SET photo_bytes=photo_bytes-OLD.bytes WHERE id=1;
END;
