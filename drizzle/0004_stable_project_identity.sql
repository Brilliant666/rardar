CREATE TRIGGER IF NOT EXISTS feedback_insert_decision_event
AFTER INSERT ON feedback
BEGIN
  INSERT INTO decision_events (device_id, project_slug, value)
  VALUES (NEW.device_id, NEW.project_slug, NEW.value);
END
--> statement-breakpoint
CREATE TRIGGER IF NOT EXISTS feedback_update_decision_event
AFTER UPDATE OF value ON feedback
WHEN OLD.value <> NEW.value
BEGIN
  INSERT INTO decision_events (device_id, project_slug, value)
  VALUES (NEW.device_id, NEW.project_slug, NEW.value);
END
--> statement-breakpoint
CREATE TABLE IF NOT EXISTS `decision_events_v2` (
	`id` integer PRIMARY KEY AUTOINCREMENT NOT NULL,
	`legacy_event_id` integer NOT NULL,
	`device_id` text NOT NULL,
	`project_id_version` integer NOT NULL,
	`project_id` text NOT NULL,
	`project_slug` text NOT NULL,
	`catalog_generation_id` text NOT NULL,
	`value` text NOT NULL,
	`occurred_at` text NOT NULL,
	CONSTRAINT "decision_events_v2_version_check" CHECK("decision_events_v2"."project_id_version" = 1),
	CONSTRAINT "decision_events_v2_value_check" CHECK("decision_events_v2"."value" IN ('有用', '无用', '复用', '待确定')),
	CONSTRAINT "decision_events_v2_time_check" CHECK(julianday("decision_events_v2"."occurred_at") IS NOT NULL)
);
--> statement-breakpoint
CREATE UNIQUE INDEX IF NOT EXISTS `decision_events_v2_legacy_event_idx` ON `decision_events_v2` (`legacy_event_id`);--> statement-breakpoint
CREATE INDEX IF NOT EXISTS `decision_events_v2_device_occurred_idx` ON `decision_events_v2` (`device_id`,`occurred_at`);--> statement-breakpoint
CREATE INDEX IF NOT EXISTS `decision_events_v2_device_project_occurred_idx` ON `decision_events_v2` (`device_id`,`project_id`,`occurred_at`);--> statement-breakpoint
CREATE TABLE IF NOT EXISTS `feedback_v2` (
	`device_id` text NOT NULL,
	`project_id_version` integer NOT NULL,
	`project_id` text NOT NULL,
	`project_slug` text NOT NULL,
	`catalog_generation_id` text NOT NULL,
	`value` text NOT NULL,
	`created_at` text NOT NULL,
	`updated_at` text NOT NULL,
	PRIMARY KEY(`device_id`, `project_id`),
	CONSTRAINT "feedback_v2_version_check" CHECK("feedback_v2"."project_id_version" = 1),
	CONSTRAINT "feedback_v2_value_check" CHECK("feedback_v2"."value" IN ('有用', '无用', '复用', '待确定')),
	CONSTRAINT "feedback_v2_created_time_check" CHECK(julianday("feedback_v2"."created_at") IS NOT NULL),
	CONSTRAINT "feedback_v2_updated_time_check" CHECK(julianday("feedback_v2"."updated_at") IS NOT NULL)
);
--> statement-breakpoint
CREATE INDEX IF NOT EXISTS `feedback_v2_device_updated_idx` ON `feedback_v2` (`device_id`,`updated_at`);--> statement-breakpoint
CREATE TABLE IF NOT EXISTS `project_action_events_v2` (
	`id` integer PRIMARY KEY AUTOINCREMENT NOT NULL,
	`device_id` text NOT NULL,
	`project_id_version` integer NOT NULL,
	`project_id` text NOT NULL,
	`project_slug` text NOT NULL,
	`catalog_generation_id` text NOT NULL,
	`action` text NOT NULL,
	`occurred_at` text DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')) NOT NULL,
	`idempotency_key` text NOT NULL,
	CONSTRAINT "project_action_events_v2_version_check" CHECK("project_action_events_v2"."project_id_version" = 1),
	CONSTRAINT "project_action_events_v2_action_check" CHECK("project_action_events_v2"."action" IN ('opened', 'saved', 'tried', 'cloned', 'reused')),
	CONSTRAINT "project_action_events_v2_time_check" CHECK(julianday("project_action_events_v2"."occurred_at") IS NOT NULL)
);
--> statement-breakpoint
CREATE UNIQUE INDEX IF NOT EXISTS `project_action_events_v2_device_idempotency_idx` ON `project_action_events_v2` (`device_id`,`idempotency_key`);--> statement-breakpoint
CREATE INDEX IF NOT EXISTS `project_action_events_v2_device_occurred_idx` ON `project_action_events_v2` (`device_id`,`occurred_at`);--> statement-breakpoint
CREATE INDEX IF NOT EXISTS `project_action_events_v2_device_project_occurred_idx` ON `project_action_events_v2` (`device_id`,`project_id`,`occurred_at`);--> statement-breakpoint
CREATE TABLE IF NOT EXISTS `project_action_state_v2` (
	`device_id` text NOT NULL,
	`project_id_version` integer NOT NULL,
	`project_id` text NOT NULL,
	`project_slug` text NOT NULL,
	`catalog_generation_id` text NOT NULL,
	`highest_stage` text NOT NULL,
	`opened_at` text,
	`saved_at` text,
	`tried_at` text,
	`cloned_at` text,
	`reused_at` text,
	`updated_at` text NOT NULL,
	PRIMARY KEY(`device_id`, `project_id`),
	CONSTRAINT "project_action_state_v2_version_check" CHECK("project_action_state_v2"."project_id_version" = 1),
	CONSTRAINT "project_action_state_v2_stage_check" CHECK("project_action_state_v2"."highest_stage" IN ('opened', 'saved', 'tried', 'cloned', 'reused'))
);
--> statement-breakpoint
CREATE INDEX IF NOT EXISTS `project_action_state_v2_device_updated_idx` ON `project_action_state_v2` (`device_id`,`updated_at`);--> statement-breakpoint
CREATE TABLE IF NOT EXISTS `project_identity_catalog` (
	`generation_id` text NOT NULL,
	`project_id_version` integer NOT NULL,
	`project_id` text NOT NULL,
	`canonical_repository` text NOT NULL,
	`project_slug` text NOT NULL,
	PRIMARY KEY(`generation_id`, `project_id`),
	CONSTRAINT "project_identity_catalog_version_check" CHECK("project_identity_catalog"."project_id_version" = 1)
);
--> statement-breakpoint
CREATE UNIQUE INDEX IF NOT EXISTS `project_identity_catalog_generation_repository_idx` ON `project_identity_catalog` (`generation_id`,`canonical_repository`);--> statement-breakpoint
CREATE INDEX IF NOT EXISTS `project_identity_catalog_generation_slug_idx` ON `project_identity_catalog` (`generation_id`,`project_slug`);--> statement-breakpoint
CREATE TRIGGER IF NOT EXISTS project_identity_catalog_reject_global_collision
    BEFORE INSERT ON project_identity_catalog
    WHEN EXISTS (
      SELECT 1 FROM project_identity_catalog AS existing
      WHERE (existing.project_id = NEW.project_id
          AND existing.canonical_repository <> NEW.canonical_repository)
        OR (existing.canonical_repository = NEW.canonical_repository
          AND existing.project_id <> NEW.project_id)
        OR (existing.project_slug = NEW.project_slug
          AND existing.project_id <> NEW.project_id)
    )
    BEGIN SELECT RAISE(ABORT, 'stable project identity collision'); END
--> statement-breakpoint
CREATE TRIGGER IF NOT EXISTS project_identity_catalog_reject_replacement
    BEFORE INSERT ON project_identity_catalog
    WHEN EXISTS (
      SELECT 1 FROM project_identity_catalog AS existing
      WHERE existing.generation_id = NEW.generation_id
        AND (existing.project_id = NEW.project_id
          OR existing.canonical_repository = NEW.canonical_repository)
        AND NOT (
          existing.project_id_version = NEW.project_id_version
          AND existing.project_id = NEW.project_id
          AND existing.canonical_repository = NEW.canonical_repository
          AND existing.project_slug = NEW.project_slug
        )
    )
    BEGIN SELECT RAISE(ABORT, 'project_identity_catalog mapping is immutable'); END
--> statement-breakpoint
CREATE TABLE IF NOT EXISTS `project_identity_migration_guard` (
	`failure` integer NOT NULL,
	CONSTRAINT "project_identity_migration_guard_check" CHECK("project_identity_migration_guard"."failure" = 0)
);
--> statement-breakpoint
CREATE TABLE IF NOT EXISTS `project_identity_runtime` (
	`singleton` integer PRIMARY KEY NOT NULL,
	`generation_id` text NOT NULL,
	`published_at` text NOT NULL,
	`published_at_micros` integer NOT NULL,
	CONSTRAINT "project_identity_runtime_singleton_check" CHECK("project_identity_runtime"."singleton" = 1),
	CONSTRAINT "project_identity_runtime_published_time_check" CHECK(julianday("project_identity_runtime"."published_at") IS NOT NULL)
);
--> statement-breakpoint
CREATE TRIGGER IF NOT EXISTS project_identity_catalog_reject_update
    BEFORE UPDATE ON project_identity_catalog
    BEGIN SELECT RAISE(ABORT, 'project_identity_catalog is immutable'); END
--> statement-breakpoint
CREATE TRIGGER IF NOT EXISTS project_identity_catalog_reject_delete
    BEFORE DELETE ON project_identity_catalog
    BEGIN SELECT RAISE(ABORT, 'project_identity_catalog is immutable'); END
--> statement-breakpoint
CREATE TRIGGER IF NOT EXISTS project_action_events_v2_validate_mapping
    BEFORE INSERT ON project_action_events_v2
    WHEN NOT EXISTS (
      SELECT 1 FROM project_identity_catalog AS identity
      WHERE identity.generation_id = NEW.catalog_generation_id
        AND identity.project_id_version = NEW.project_id_version
        AND identity.project_id = NEW.project_id
        AND identity.project_slug = NEW.project_slug
    )
    BEGIN SELECT RAISE(ABORT, 'unknown stable project identity'); END
--> statement-breakpoint
CREATE TRIGGER IF NOT EXISTS project_action_events_v2_validate_active_generation
    BEFORE INSERT ON project_action_events_v2
    WHEN NOT EXISTS (
      SELECT 1 FROM project_identity_runtime AS runtime
      WHERE runtime.singleton = 1
        AND runtime.generation_id = NEW.catalog_generation_id
    )
    BEGIN SELECT RAISE(ABORT, 'stale stable project generation'); END
--> statement-breakpoint
CREATE TRIGGER IF NOT EXISTS project_action_events_v2_reject_legacy_conflict
    BEFORE INSERT ON project_action_events_v2
    WHEN EXISTS (
      SELECT 1 FROM project_action_events AS legacy
      WHERE legacy.device_id = NEW.device_id
        AND legacy.idempotency_key = NEW.idempotency_key
        AND (legacy.project_slug <> NEW.project_slug OR legacy.action <> NEW.action)
    )
    BEGIN SELECT RAISE(ABORT, 'project action idempotency conflict'); END
--> statement-breakpoint
CREATE TRIGGER IF NOT EXISTS project_action_events_reject_stable_conflict
    BEFORE INSERT ON project_action_events
    WHEN EXISTS (
      SELECT 1 FROM project_action_events_v2 AS stable
      WHERE stable.device_id = NEW.device_id
        AND stable.idempotency_key = NEW.idempotency_key
        AND (stable.project_slug <> NEW.project_slug OR stable.action <> NEW.action)
    )
    BEGIN SELECT RAISE(ABORT, 'project action idempotency conflict'); END
--> statement-breakpoint
CREATE TRIGGER IF NOT EXISTS project_action_events_v2_sync_state
  AFTER INSERT ON project_action_events_v2
  BEGIN
    INSERT INTO project_action_state_v2 (
      device_id, project_id_version, project_id, project_slug,
      catalog_generation_id, highest_stage, opened_at, saved_at,
      tried_at, cloned_at, reused_at, updated_at
    ) VALUES (
      NEW.device_id, NEW.project_id_version, NEW.project_id, NEW.project_slug,
      NEW.catalog_generation_id, NEW.action,
      CASE WHEN NEW.action = 'opened' THEN NEW.occurred_at END,
      CASE WHEN NEW.action = 'saved' THEN NEW.occurred_at END,
      CASE WHEN NEW.action = 'tried' THEN NEW.occurred_at END,
      CASE WHEN NEW.action = 'cloned' THEN NEW.occurred_at END,
      CASE WHEN NEW.action = 'reused' THEN NEW.occurred_at END,
      NEW.occurred_at
    )
    ON CONFLICT (device_id, project_id) DO UPDATE SET
      project_id_version = excluded.project_id_version,
      project_slug = CASE
        WHEN julianday(excluded.updated_at) >= julianday(project_action_state_v2.updated_at)
          THEN excluded.project_slug
        ELSE project_action_state_v2.project_slug
      END,
      catalog_generation_id = CASE
        WHEN julianday(excluded.updated_at) >= julianday(project_action_state_v2.updated_at)
          THEN excluded.catalog_generation_id
        ELSE project_action_state_v2.catalog_generation_id
      END,
      highest_stage = CASE
        WHEN CASE excluded.highest_stage
  WHEN 'opened' THEN 1 WHEN 'saved' THEN 2 WHEN 'tried' THEN 3
  WHEN 'cloned' THEN 4 WHEN 'reused' THEN 5 ELSE 0 END > CASE project_action_state_v2.highest_stage
  WHEN 'opened' THEN 1 WHEN 'saved' THEN 2 WHEN 'tried' THEN 3
  WHEN 'cloned' THEN 4 WHEN 'reused' THEN 5 ELSE 0 END
          THEN excluded.highest_stage
        ELSE project_action_state_v2.highest_stage
      END,
      opened_at = CASE
  WHEN excluded.opened_at IS NULL THEN project_action_state_v2.opened_at
  WHEN project_action_state_v2.opened_at IS NULL
    OR julianday(excluded.opened_at) >= julianday(project_action_state_v2.opened_at)
    THEN excluded.opened_at
  ELSE project_action_state_v2.opened_at
END,
      saved_at = CASE
  WHEN excluded.saved_at IS NULL THEN project_action_state_v2.saved_at
  WHEN project_action_state_v2.saved_at IS NULL
    OR julianday(excluded.saved_at) >= julianday(project_action_state_v2.saved_at)
    THEN excluded.saved_at
  ELSE project_action_state_v2.saved_at
END,
      tried_at = CASE
  WHEN excluded.tried_at IS NULL THEN project_action_state_v2.tried_at
  WHEN project_action_state_v2.tried_at IS NULL
    OR julianday(excluded.tried_at) >= julianday(project_action_state_v2.tried_at)
    THEN excluded.tried_at
  ELSE project_action_state_v2.tried_at
END,
      cloned_at = CASE
  WHEN excluded.cloned_at IS NULL THEN project_action_state_v2.cloned_at
  WHEN project_action_state_v2.cloned_at IS NULL
    OR julianday(excluded.cloned_at) >= julianday(project_action_state_v2.cloned_at)
    THEN excluded.cloned_at
  ELSE project_action_state_v2.cloned_at
END,
      reused_at = CASE
  WHEN excluded.reused_at IS NULL THEN project_action_state_v2.reused_at
  WHEN project_action_state_v2.reused_at IS NULL
    OR julianday(excluded.reused_at) >= julianday(project_action_state_v2.reused_at)
    THEN excluded.reused_at
  ELSE project_action_state_v2.reused_at
END,
      updated_at = CASE
  WHEN excluded.updated_at IS NULL THEN project_action_state_v2.updated_at
  WHEN project_action_state_v2.updated_at IS NULL
    OR julianday(excluded.updated_at) >= julianday(project_action_state_v2.updated_at)
    THEN excluded.updated_at
  ELSE project_action_state_v2.updated_at
END;
  END
--> statement-breakpoint
CREATE TRIGGER IF NOT EXISTS project_action_state_v2_validate_mapping_insert
    BEFORE INSERT ON project_action_state_v2
    WHEN NOT EXISTS (
      SELECT 1 FROM project_identity_catalog AS identity
      WHERE identity.generation_id = NEW.catalog_generation_id
        AND identity.project_id_version = NEW.project_id_version
        AND identity.project_id = NEW.project_id
        AND identity.project_slug = NEW.project_slug
    )
    BEGIN SELECT RAISE(ABORT, 'unknown stable project identity'); END
--> statement-breakpoint
CREATE TRIGGER IF NOT EXISTS project_action_state_v2_validate_active_generation_insert
    BEFORE INSERT ON project_action_state_v2
    WHEN NOT EXISTS (
      SELECT 1 FROM project_identity_runtime AS runtime
      WHERE runtime.singleton = 1
        AND runtime.generation_id = NEW.catalog_generation_id
    )
    BEGIN SELECT RAISE(ABORT, 'stale stable project generation'); END
--> statement-breakpoint
CREATE TRIGGER IF NOT EXISTS project_action_state_v2_validate_mapping_update
    BEFORE UPDATE OF project_id_version, project_id, project_slug, catalog_generation_id
    ON project_action_state_v2
    WHEN NOT EXISTS (
      SELECT 1 FROM project_identity_catalog AS identity
      WHERE identity.generation_id = NEW.catalog_generation_id
        AND identity.project_id_version = NEW.project_id_version
        AND identity.project_id = NEW.project_id
        AND identity.project_slug = NEW.project_slug
    )
    BEGIN SELECT RAISE(ABORT, 'unknown stable project identity'); END
--> statement-breakpoint
CREATE TRIGGER IF NOT EXISTS project_action_state_v2_validate_active_generation_update
    BEFORE UPDATE ON project_action_state_v2
    WHEN NOT EXISTS (
      SELECT 1 FROM project_identity_runtime AS runtime
      WHERE runtime.singleton = 1
        AND runtime.generation_id = NEW.catalog_generation_id
    )
    BEGIN SELECT RAISE(ABORT, 'stale stable project generation'); END
--> statement-breakpoint
CREATE TRIGGER IF NOT EXISTS project_action_state_v2_reject_identity_update
    BEFORE UPDATE OF device_id, project_id_version, project_id ON project_action_state_v2
    WHEN OLD.device_id <> NEW.device_id
      OR OLD.project_id_version <> NEW.project_id_version
      OR OLD.project_id <> NEW.project_id
    BEGIN SELECT RAISE(ABORT, 'project_action_state_v2 identity is immutable'); END
--> statement-breakpoint
CREATE TRIGGER IF NOT EXISTS project_action_state_v2_validate_time_insert
    BEFORE INSERT ON project_action_state_v2
    WHEN julianday(NEW.updated_at) IS NULL
      OR (NEW.opened_at IS NOT NULL AND julianday(NEW.opened_at) IS NULL)
      OR (NEW.saved_at IS NOT NULL AND julianday(NEW.saved_at) IS NULL)
      OR (NEW.tried_at IS NOT NULL AND julianday(NEW.tried_at) IS NULL)
      OR (NEW.cloned_at IS NOT NULL AND julianday(NEW.cloned_at) IS NULL)
      OR (NEW.reused_at IS NOT NULL AND julianday(NEW.reused_at) IS NULL)
    BEGIN SELECT RAISE(ABORT, 'project_action_state_v2 has an invalid timestamp'); END
--> statement-breakpoint
CREATE TRIGGER IF NOT EXISTS project_action_state_v2_validate_time_update
    BEFORE UPDATE ON project_action_state_v2
    WHEN julianday(NEW.updated_at) IS NULL
      OR (NEW.opened_at IS NOT NULL AND julianday(NEW.opened_at) IS NULL)
      OR (NEW.saved_at IS NOT NULL AND julianday(NEW.saved_at) IS NULL)
      OR (NEW.tried_at IS NOT NULL AND julianday(NEW.tried_at) IS NULL)
      OR (NEW.cloned_at IS NOT NULL AND julianday(NEW.cloned_at) IS NULL)
      OR (NEW.reused_at IS NOT NULL AND julianday(NEW.reused_at) IS NULL)
    BEGIN SELECT RAISE(ABORT, 'project_action_state_v2 has an invalid timestamp'); END
--> statement-breakpoint
CREATE TRIGGER IF NOT EXISTS project_action_state_v2_validate_projection_insert
    AFTER INSERT ON project_action_state_v2
    WHEN
  NOT EXISTS (
    SELECT 1 FROM project_action_events_v2 AS event
    WHERE event.device_id = NEW.device_id
      AND event.project_id = NEW.project_id
  )
  OR CASE NEW.highest_stage
  WHEN 'opened' THEN 1 WHEN 'saved' THEN 2 WHEN 'tried' THEN 3
  WHEN 'cloned' THEN 4 WHEN 'reused' THEN 5 ELSE 0 END <> (
    SELECT MAX(CASE event.action
  WHEN 'opened' THEN 1 WHEN 'saved' THEN 2 WHEN 'tried' THEN 3
  WHEN 'cloned' THEN 4 WHEN 'reused' THEN 5 ELSE 0 END)
    FROM project_action_events_v2 AS event
    WHERE event.device_id = NEW.device_id
      AND event.project_id = NEW.project_id
  )
  OR (NEW.opened_at IS NULL) <> NOT EXISTS (
    SELECT 1 FROM project_action_events_v2 AS event
    WHERE event.device_id = NEW.device_id
      AND event.project_id = NEW.project_id AND event.action = 'opened'
  )
  OR (NEW.opened_at IS NOT NULL AND julianday(NEW.opened_at) <> (
    SELECT MAX(julianday(event.occurred_at)) FROM project_action_events_v2 AS event
    WHERE event.device_id = NEW.device_id
      AND event.project_id = NEW.project_id AND event.action = 'opened'
  ))
  OR (NEW.saved_at IS NULL) <> NOT EXISTS (
    SELECT 1 FROM project_action_events_v2 AS event
    WHERE event.device_id = NEW.device_id
      AND event.project_id = NEW.project_id AND event.action = 'saved'
  )
  OR (NEW.saved_at IS NOT NULL AND julianday(NEW.saved_at) <> (
    SELECT MAX(julianday(event.occurred_at)) FROM project_action_events_v2 AS event
    WHERE event.device_id = NEW.device_id
      AND event.project_id = NEW.project_id AND event.action = 'saved'
  ))
  OR (NEW.tried_at IS NULL) <> NOT EXISTS (
    SELECT 1 FROM project_action_events_v2 AS event
    WHERE event.device_id = NEW.device_id
      AND event.project_id = NEW.project_id AND event.action = 'tried'
  )
  OR (NEW.tried_at IS NOT NULL AND julianday(NEW.tried_at) <> (
    SELECT MAX(julianday(event.occurred_at)) FROM project_action_events_v2 AS event
    WHERE event.device_id = NEW.device_id
      AND event.project_id = NEW.project_id AND event.action = 'tried'
  ))
  OR (NEW.cloned_at IS NULL) <> NOT EXISTS (
    SELECT 1 FROM project_action_events_v2 AS event
    WHERE event.device_id = NEW.device_id
      AND event.project_id = NEW.project_id AND event.action = 'cloned'
  )
  OR (NEW.cloned_at IS NOT NULL AND julianday(NEW.cloned_at) <> (
    SELECT MAX(julianday(event.occurred_at)) FROM project_action_events_v2 AS event
    WHERE event.device_id = NEW.device_id
      AND event.project_id = NEW.project_id AND event.action = 'cloned'
  ))
  OR (NEW.reused_at IS NULL) <> NOT EXISTS (
    SELECT 1 FROM project_action_events_v2 AS event
    WHERE event.device_id = NEW.device_id
      AND event.project_id = NEW.project_id AND event.action = 'reused'
  )
  OR (NEW.reused_at IS NOT NULL AND julianday(NEW.reused_at) <> (
    SELECT MAX(julianday(event.occurred_at)) FROM project_action_events_v2 AS event
    WHERE event.device_id = NEW.device_id
      AND event.project_id = NEW.project_id AND event.action = 'reused'
  ))
  OR julianday(NEW.updated_at) <> (
    SELECT MAX(julianday(event.occurred_at)) FROM project_action_events_v2 AS event
    WHERE event.device_id = NEW.device_id
      AND event.project_id = NEW.project_id
  )
    BEGIN SELECT RAISE(ABORT, 'project_action_state_v2 is not an Event projection'); END
--> statement-breakpoint
CREATE TRIGGER IF NOT EXISTS project_action_state_v2_validate_projection_update
    AFTER UPDATE ON project_action_state_v2
    WHEN
  NOT EXISTS (
    SELECT 1 FROM project_action_events_v2 AS event
    WHERE event.device_id = NEW.device_id
      AND event.project_id = NEW.project_id
  )
  OR CASE NEW.highest_stage
  WHEN 'opened' THEN 1 WHEN 'saved' THEN 2 WHEN 'tried' THEN 3
  WHEN 'cloned' THEN 4 WHEN 'reused' THEN 5 ELSE 0 END <> (
    SELECT MAX(CASE event.action
  WHEN 'opened' THEN 1 WHEN 'saved' THEN 2 WHEN 'tried' THEN 3
  WHEN 'cloned' THEN 4 WHEN 'reused' THEN 5 ELSE 0 END)
    FROM project_action_events_v2 AS event
    WHERE event.device_id = NEW.device_id
      AND event.project_id = NEW.project_id
  )
  OR (NEW.opened_at IS NULL) <> NOT EXISTS (
    SELECT 1 FROM project_action_events_v2 AS event
    WHERE event.device_id = NEW.device_id
      AND event.project_id = NEW.project_id AND event.action = 'opened'
  )
  OR (NEW.opened_at IS NOT NULL AND julianday(NEW.opened_at) <> (
    SELECT MAX(julianday(event.occurred_at)) FROM project_action_events_v2 AS event
    WHERE event.device_id = NEW.device_id
      AND event.project_id = NEW.project_id AND event.action = 'opened'
  ))
  OR (NEW.saved_at IS NULL) <> NOT EXISTS (
    SELECT 1 FROM project_action_events_v2 AS event
    WHERE event.device_id = NEW.device_id
      AND event.project_id = NEW.project_id AND event.action = 'saved'
  )
  OR (NEW.saved_at IS NOT NULL AND julianday(NEW.saved_at) <> (
    SELECT MAX(julianday(event.occurred_at)) FROM project_action_events_v2 AS event
    WHERE event.device_id = NEW.device_id
      AND event.project_id = NEW.project_id AND event.action = 'saved'
  ))
  OR (NEW.tried_at IS NULL) <> NOT EXISTS (
    SELECT 1 FROM project_action_events_v2 AS event
    WHERE event.device_id = NEW.device_id
      AND event.project_id = NEW.project_id AND event.action = 'tried'
  )
  OR (NEW.tried_at IS NOT NULL AND julianday(NEW.tried_at) <> (
    SELECT MAX(julianday(event.occurred_at)) FROM project_action_events_v2 AS event
    WHERE event.device_id = NEW.device_id
      AND event.project_id = NEW.project_id AND event.action = 'tried'
  ))
  OR (NEW.cloned_at IS NULL) <> NOT EXISTS (
    SELECT 1 FROM project_action_events_v2 AS event
    WHERE event.device_id = NEW.device_id
      AND event.project_id = NEW.project_id AND event.action = 'cloned'
  )
  OR (NEW.cloned_at IS NOT NULL AND julianday(NEW.cloned_at) <> (
    SELECT MAX(julianday(event.occurred_at)) FROM project_action_events_v2 AS event
    WHERE event.device_id = NEW.device_id
      AND event.project_id = NEW.project_id AND event.action = 'cloned'
  ))
  OR (NEW.reused_at IS NULL) <> NOT EXISTS (
    SELECT 1 FROM project_action_events_v2 AS event
    WHERE event.device_id = NEW.device_id
      AND event.project_id = NEW.project_id AND event.action = 'reused'
  )
  OR (NEW.reused_at IS NOT NULL AND julianday(NEW.reused_at) <> (
    SELECT MAX(julianday(event.occurred_at)) FROM project_action_events_v2 AS event
    WHERE event.device_id = NEW.device_id
      AND event.project_id = NEW.project_id AND event.action = 'reused'
  ))
  OR julianday(NEW.updated_at) <> (
    SELECT MAX(julianday(event.occurred_at)) FROM project_action_events_v2 AS event
    WHERE event.device_id = NEW.device_id
      AND event.project_id = NEW.project_id
  )
    BEGIN SELECT RAISE(ABORT, 'project_action_state_v2 is not an Event projection'); END
--> statement-breakpoint
CREATE TRIGGER IF NOT EXISTS project_action_state_v2_reject_delete
    BEFORE DELETE ON project_action_state_v2
    BEGIN SELECT RAISE(ABORT, 'project_action_state_v2 is an Event projection'); END
--> statement-breakpoint
CREATE TRIGGER IF NOT EXISTS project_action_events_v2_legacy_projection
    AFTER INSERT ON project_action_events_v2
    BEGIN
      INSERT INTO project_action_events (device_id, project_slug, action, occurred_at, idempotency_key)
      SELECT NEW.device_id, NEW.project_slug, NEW.action, NEW.occurred_at, NEW.idempotency_key
      WHERE NOT EXISTS (
        SELECT 1 FROM project_action_events
        WHERE device_id = NEW.device_id AND idempotency_key = NEW.idempotency_key
      );
    END
--> statement-breakpoint
CREATE TRIGGER IF NOT EXISTS project_action_events_capture_stable
    AFTER INSERT ON project_action_events
    WHEN NOT EXISTS (
      SELECT 1 FROM project_action_events_v2
      WHERE device_id = NEW.device_id AND idempotency_key = NEW.idempotency_key
    ) AND (
      SELECT COUNT(*) FROM project_identity_catalog AS identity
      JOIN project_identity_runtime AS runtime ON runtime.singleton = 1
        AND runtime.generation_id = identity.generation_id
      WHERE identity.project_slug = NEW.project_slug
    ) = 1
    BEGIN
      INSERT INTO project_action_events_v2 (
        device_id, project_id_version, project_id, project_slug,
        catalog_generation_id, action, occurred_at, idempotency_key
      )
      SELECT NEW.device_id, identity.project_id_version, identity.project_id,
        NEW.project_slug, identity.generation_id, NEW.action, NEW.occurred_at,
        NEW.idempotency_key
      FROM project_identity_catalog AS identity
      JOIN project_identity_runtime AS runtime ON runtime.singleton = 1
        AND runtime.generation_id = identity.generation_id
      WHERE identity.project_slug = NEW.project_slug;
    END
--> statement-breakpoint
CREATE TRIGGER IF NOT EXISTS project_action_events_v2_reject_identity_replacement
    BEFORE INSERT ON project_action_events_v2
    WHEN EXISTS (
      SELECT 1 FROM project_action_events_v2
      WHERE id = NEW.id OR (device_id = NEW.device_id AND idempotency_key = NEW.idempotency_key)
    )
    BEGIN SELECT RAISE(ABORT, 'project_action_events_v2 identity is immutable'); END
--> statement-breakpoint
CREATE TRIGGER IF NOT EXISTS project_action_events_v2_reject_update
    BEFORE UPDATE ON project_action_events_v2
    BEGIN SELECT RAISE(ABORT, 'project_action_events_v2 is append-only'); END
--> statement-breakpoint
CREATE TRIGGER IF NOT EXISTS project_action_events_v2_reject_delete
    BEFORE DELETE ON project_action_events_v2
    BEGIN SELECT RAISE(ABORT, 'project_action_events_v2 is append-only'); END
--> statement-breakpoint
CREATE TRIGGER IF NOT EXISTS feedback_v2_validate_mapping
    BEFORE INSERT ON feedback_v2
    WHEN NOT EXISTS (
      SELECT 1 FROM project_identity_catalog AS identity
      WHERE identity.generation_id = NEW.catalog_generation_id
        AND identity.project_id_version = NEW.project_id_version
        AND identity.project_id = NEW.project_id
        AND identity.project_slug = NEW.project_slug
    )
    BEGIN SELECT RAISE(ABORT, 'unknown stable project identity'); END
--> statement-breakpoint
CREATE TRIGGER IF NOT EXISTS feedback_v2_validate_active_generation
    BEFORE INSERT ON feedback_v2
    WHEN NOT EXISTS (
      SELECT 1 FROM project_identity_runtime AS runtime
      WHERE runtime.singleton = 1
        AND runtime.generation_id = NEW.catalog_generation_id
    )
    BEGIN SELECT RAISE(ABORT, 'stale stable project generation'); END
--> statement-breakpoint
CREATE TRIGGER IF NOT EXISTS feedback_v2_validate_mapping_update
    BEFORE UPDATE OF project_id_version, project_id, project_slug, catalog_generation_id
    ON feedback_v2
    WHEN NOT EXISTS (
      SELECT 1 FROM project_identity_catalog AS identity
      WHERE identity.generation_id = NEW.catalog_generation_id
        AND identity.project_id_version = NEW.project_id_version
        AND identity.project_id = NEW.project_id
        AND identity.project_slug = NEW.project_slug
    )
    BEGIN SELECT RAISE(ABORT, 'unknown stable project identity'); END
--> statement-breakpoint
CREATE TRIGGER IF NOT EXISTS feedback_v2_validate_active_generation_update
    BEFORE UPDATE ON feedback_v2
    WHEN NOT EXISTS (
      SELECT 1 FROM project_identity_runtime AS runtime
      WHERE runtime.singleton = 1
        AND runtime.generation_id = NEW.catalog_generation_id
    )
    BEGIN SELECT RAISE(ABORT, 'stale stable project generation'); END
--> statement-breakpoint
CREATE TRIGGER IF NOT EXISTS feedback_v2_reject_identity_update
    BEFORE UPDATE OF device_id, project_id_version, project_id ON feedback_v2
    WHEN OLD.device_id <> NEW.device_id
      OR OLD.project_id_version <> NEW.project_id_version
      OR OLD.project_id <> NEW.project_id
    BEGIN SELECT RAISE(ABORT, 'feedback_v2 identity is immutable'); END
--> statement-breakpoint
CREATE TRIGGER IF NOT EXISTS feedback_v2_reject_delete
    BEFORE DELETE ON feedback_v2
    BEGIN SELECT RAISE(ABORT, 'feedback_v2 State cannot be deleted'); END
--> statement-breakpoint
CREATE TRIGGER IF NOT EXISTS feedback_v2_legacy_projection_insert
    AFTER INSERT ON feedback_v2
    BEGIN
      INSERT INTO feedback (device_id, project_slug, value, created_at, updated_at)
      VALUES (NEW.device_id, NEW.project_slug, NEW.value, NEW.created_at, NEW.updated_at)
      ON CONFLICT (device_id, project_slug) DO UPDATE SET
        value = excluded.value, updated_at = excluded.updated_at
      WHERE feedback.value <> excluded.value;
    END
--> statement-breakpoint
CREATE TRIGGER IF NOT EXISTS feedback_v2_legacy_projection_update
    AFTER UPDATE OF value ON feedback_v2
    WHEN OLD.value <> NEW.value
    BEGIN
      INSERT INTO feedback (device_id, project_slug, value, created_at, updated_at)
      VALUES (NEW.device_id, NEW.project_slug, NEW.value, NEW.created_at, NEW.updated_at)
      ON CONFLICT (device_id, project_slug) DO UPDATE SET
        value = excluded.value, updated_at = excluded.updated_at
      WHERE feedback.value <> excluded.value;
    END
--> statement-breakpoint
CREATE TRIGGER IF NOT EXISTS feedback_insert_stable_capture
    AFTER INSERT ON feedback
    WHEN (
      SELECT COUNT(*) FROM project_identity_catalog AS identity
      JOIN project_identity_runtime AS runtime ON runtime.singleton = 1
        AND runtime.generation_id = identity.generation_id
      WHERE identity.project_slug = NEW.project_slug
    ) = 1
    BEGIN
      INSERT INTO feedback_v2 (
        device_id, project_id_version, project_id, project_slug,
        catalog_generation_id, value, created_at, updated_at
      )
      SELECT NEW.device_id, identity.project_id_version, identity.project_id,
        NEW.project_slug, identity.generation_id, NEW.value, NEW.created_at, NEW.updated_at
      FROM project_identity_catalog AS identity
      JOIN project_identity_runtime AS runtime ON runtime.singleton = 1
        AND runtime.generation_id = identity.generation_id
      WHERE identity.project_slug = NEW.project_slug
      ON CONFLICT (device_id, project_id) DO UPDATE SET
        project_slug = excluded.project_slug,
        catalog_generation_id = excluded.catalog_generation_id,
        value = excluded.value,
        updated_at = excluded.updated_at
      WHERE feedback_v2.value <> excluded.value;
    END
--> statement-breakpoint
CREATE TRIGGER IF NOT EXISTS feedback_update_stable_capture
    AFTER UPDATE OF value ON feedback
    WHEN OLD.value <> NEW.value AND (
      SELECT COUNT(*) FROM project_identity_catalog AS identity
      JOIN project_identity_runtime AS runtime ON runtime.singleton = 1
        AND runtime.generation_id = identity.generation_id
      WHERE identity.project_slug = NEW.project_slug
    ) = 1
    BEGIN
      INSERT INTO feedback_v2 (
        device_id, project_id_version, project_id, project_slug,
        catalog_generation_id, value, created_at, updated_at
      )
      SELECT NEW.device_id, identity.project_id_version, identity.project_id,
        NEW.project_slug, identity.generation_id, NEW.value, NEW.created_at, NEW.updated_at
      FROM project_identity_catalog AS identity
      JOIN project_identity_runtime AS runtime ON runtime.singleton = 1
        AND runtime.generation_id = identity.generation_id
      WHERE identity.project_slug = NEW.project_slug
      ON CONFLICT (device_id, project_id) DO UPDATE SET
        project_slug = excluded.project_slug,
        catalog_generation_id = excluded.catalog_generation_id,
        value = excluded.value,
        updated_at = excluded.updated_at
      WHERE feedback_v2.value <> excluded.value;
    END
--> statement-breakpoint
CREATE TRIGGER IF NOT EXISTS decision_events_stable_capture
    AFTER INSERT ON decision_events
    WHEN (
      SELECT COUNT(*) FROM project_identity_catalog AS identity
      JOIN project_identity_runtime AS runtime ON runtime.singleton = 1
        AND runtime.generation_id = identity.generation_id
      WHERE identity.project_slug = NEW.project_slug
    ) = 1
    BEGIN
      INSERT INTO decision_events_v2 (
        legacy_event_id, device_id, project_id_version, project_id,
        project_slug, catalog_generation_id, value, occurred_at
      )
      SELECT NEW.id, NEW.device_id, identity.project_id_version, identity.project_id,
        NEW.project_slug, identity.generation_id, NEW.value, NEW.created_at
      FROM project_identity_catalog AS identity
      JOIN project_identity_runtime AS runtime ON runtime.singleton = 1
        AND runtime.generation_id = identity.generation_id
      WHERE identity.project_slug = NEW.project_slug
        AND NOT EXISTS (
          SELECT 1 FROM decision_events_v2
          WHERE legacy_event_id = NEW.id
        );
    END
--> statement-breakpoint
CREATE TRIGGER IF NOT EXISTS decision_events_v2_validate_mapping
    BEFORE INSERT ON decision_events_v2
    WHEN NOT EXISTS (
      SELECT 1 FROM project_identity_catalog AS identity
      WHERE identity.generation_id = NEW.catalog_generation_id
        AND identity.project_id_version = NEW.project_id_version
        AND identity.project_id = NEW.project_id
        AND identity.project_slug = NEW.project_slug
    )
    BEGIN SELECT RAISE(ABORT, 'unknown stable project identity'); END
--> statement-breakpoint
CREATE TRIGGER IF NOT EXISTS decision_events_v2_validate_active_generation
    BEFORE INSERT ON decision_events_v2
    WHEN NOT EXISTS (
      SELECT 1 FROM project_identity_runtime AS runtime
      WHERE runtime.singleton = 1
        AND runtime.generation_id = NEW.catalog_generation_id
    )
    BEGIN SELECT RAISE(ABORT, 'stale stable project generation'); END
--> statement-breakpoint
CREATE TRIGGER IF NOT EXISTS decision_events_v2_reject_identity_replacement
    BEFORE INSERT ON decision_events_v2
    WHEN EXISTS (
      SELECT 1 FROM decision_events_v2
      WHERE id = NEW.id OR legacy_event_id = NEW.legacy_event_id
    )
    BEGIN SELECT RAISE(ABORT, 'decision_events_v2 identity is immutable'); END
--> statement-breakpoint
CREATE TRIGGER IF NOT EXISTS decision_events_v2_reject_update
    BEFORE UPDATE ON decision_events_v2
    BEGIN SELECT RAISE(ABORT, 'decision_events_v2 is append-only'); END
--> statement-breakpoint
CREATE TRIGGER IF NOT EXISTS decision_events_v2_reject_delete
    BEFORE DELETE ON decision_events_v2
    BEGIN SELECT RAISE(ABORT, 'decision_events_v2 is append-only'); END
