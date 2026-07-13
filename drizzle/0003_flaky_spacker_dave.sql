CREATE TABLE IF NOT EXISTS `project_action_events` (
	`id` integer PRIMARY KEY AUTOINCREMENT NOT NULL,
	`device_id` text NOT NULL,
	`project_slug` text NOT NULL,
	`action` text NOT NULL,
	`occurred_at` text DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')) NOT NULL,
	`idempotency_key` text NOT NULL,
	CONSTRAINT "project_action_events_action_check" CHECK("project_action_events"."action" IN ('opened', 'saved', 'tried', 'cloned', 'reused')),
	CONSTRAINT "project_action_events_time_check" CHECK(julianday("project_action_events"."occurred_at") IS NOT NULL)
);
--> statement-breakpoint
CREATE UNIQUE INDEX IF NOT EXISTS `project_action_events_device_idempotency_idx` ON `project_action_events` (`device_id`,`idempotency_key`);--> statement-breakpoint
CREATE INDEX IF NOT EXISTS `project_action_events_device_occurred_idx` ON `project_action_events` (`device_id`,`occurred_at`);--> statement-breakpoint
CREATE INDEX IF NOT EXISTS `project_action_events_device_project_occurred_idx` ON `project_action_events` (`device_id`,`project_slug`,`occurred_at`);--> statement-breakpoint
CREATE TABLE IF NOT EXISTS `project_action_state` (
	`device_id` text NOT NULL,
	`project_slug` text NOT NULL,
	`highest_stage` text NOT NULL,
	`opened_at` text,
	`saved_at` text,
	`tried_at` text,
	`cloned_at` text,
	`reused_at` text,
	`updated_at` text NOT NULL,
	PRIMARY KEY(`device_id`, `project_slug`),
	CONSTRAINT "project_action_state_stage_check" CHECK("project_action_state"."highest_stage" IN ('opened', 'saved', 'tried', 'cloned', 'reused'))
);
--> statement-breakpoint
CREATE INDEX IF NOT EXISTS `project_action_state_device_updated_idx` ON `project_action_state` (`device_id`,`updated_at`);
--> statement-breakpoint
CREATE TRIGGER IF NOT EXISTS `project_action_events_sync_state`
AFTER INSERT ON `project_action_events`
BEGIN
	INSERT INTO `project_action_state` (
		`device_id`,
		`project_slug`,
		`highest_stage`,
		`opened_at`,
		`saved_at`,
		`tried_at`,
		`cloned_at`,
		`reused_at`,
		`updated_at`
	) VALUES (
		NEW.`device_id`,
		NEW.`project_slug`,
		NEW.`action`,
		CASE WHEN NEW.`action` = 'opened' THEN NEW.`occurred_at` END,
		CASE WHEN NEW.`action` = 'saved' THEN NEW.`occurred_at` END,
		CASE WHEN NEW.`action` = 'tried' THEN NEW.`occurred_at` END,
		CASE WHEN NEW.`action` = 'cloned' THEN NEW.`occurred_at` END,
		CASE WHEN NEW.`action` = 'reused' THEN NEW.`occurred_at` END,
		NEW.`occurred_at`
	)
	ON CONFLICT (`device_id`, `project_slug`) DO UPDATE SET
		`highest_stage` = CASE
			WHEN CASE excluded.`highest_stage`
				WHEN 'opened' THEN 1 WHEN 'saved' THEN 2 WHEN 'tried' THEN 3
				WHEN 'cloned' THEN 4 WHEN 'reused' THEN 5 ELSE 0 END
			> CASE `project_action_state`.`highest_stage`
				WHEN 'opened' THEN 1 WHEN 'saved' THEN 2 WHEN 'tried' THEN 3
				WHEN 'cloned' THEN 4 WHEN 'reused' THEN 5 ELSE 0 END
			THEN excluded.`highest_stage`
			ELSE `project_action_state`.`highest_stage`
		END,
		`opened_at` = CASE
			WHEN excluded.`opened_at` IS NULL THEN `project_action_state`.`opened_at`
			WHEN `project_action_state`.`opened_at` IS NULL
				OR julianday(excluded.`opened_at`) >= julianday(`project_action_state`.`opened_at`)
				THEN excluded.`opened_at`
			ELSE `project_action_state`.`opened_at`
		END,
		`saved_at` = CASE
			WHEN excluded.`saved_at` IS NULL THEN `project_action_state`.`saved_at`
			WHEN `project_action_state`.`saved_at` IS NULL
				OR julianday(excluded.`saved_at`) >= julianday(`project_action_state`.`saved_at`)
				THEN excluded.`saved_at`
			ELSE `project_action_state`.`saved_at`
		END,
		`tried_at` = CASE
			WHEN excluded.`tried_at` IS NULL THEN `project_action_state`.`tried_at`
			WHEN `project_action_state`.`tried_at` IS NULL
				OR julianday(excluded.`tried_at`) >= julianday(`project_action_state`.`tried_at`)
				THEN excluded.`tried_at`
			ELSE `project_action_state`.`tried_at`
		END,
		`cloned_at` = CASE
			WHEN excluded.`cloned_at` IS NULL THEN `project_action_state`.`cloned_at`
			WHEN `project_action_state`.`cloned_at` IS NULL
				OR julianday(excluded.`cloned_at`) >= julianday(`project_action_state`.`cloned_at`)
				THEN excluded.`cloned_at`
			ELSE `project_action_state`.`cloned_at`
		END,
		`reused_at` = CASE
			WHEN excluded.`reused_at` IS NULL THEN `project_action_state`.`reused_at`
			WHEN `project_action_state`.`reused_at` IS NULL
				OR julianday(excluded.`reused_at`) >= julianday(`project_action_state`.`reused_at`)
				THEN excluded.`reused_at`
			ELSE `project_action_state`.`reused_at`
		END,
		`updated_at` = CASE
			WHEN julianday(excluded.`updated_at`) >= julianday(`project_action_state`.`updated_at`)
				THEN excluded.`updated_at`
			ELSE `project_action_state`.`updated_at`
		END;
END;
--> statement-breakpoint
CREATE TRIGGER IF NOT EXISTS `project_action_events_legacy_projection`
AFTER INSERT ON `project_action_events`
BEGIN
	INSERT INTO `project_actions` (`device_id`, `project_slug`, `action`, `created_at`)
	VALUES (
		NEW.`device_id`,
		NEW.`project_slug`,
		NEW.`action`,
		strftime('%Y-%m-%d %H:%M:%f', NEW.`occurred_at`)
	)
	ON CONFLICT (`device_id`, `project_slug`, `action`) DO UPDATE SET
		`created_at` = CASE
			WHEN julianday(`project_actions`.`created_at`) IS NULL
				OR julianday(excluded.`created_at`) >= julianday(`project_actions`.`created_at`)
				THEN excluded.`created_at`
			ELSE `project_actions`.`created_at`
		END;
END;
--> statement-breakpoint
CREATE TRIGGER IF NOT EXISTS `project_actions_capture_legacy_event`
AFTER INSERT ON `project_actions`
WHEN NOT EXISTS (
	SELECT 1
	FROM `project_action_events`
	WHERE `device_id` = NEW.`device_id`
		AND `project_slug` = NEW.`project_slug`
		AND `action` = NEW.`action`
		AND ABS(julianday(`occurred_at`) - julianday(NEW.`created_at`)) < (2.0 / 86400000.0)
)
BEGIN
	INSERT INTO `project_action_events` (
		`device_id`, `project_slug`, `action`, `occurred_at`, `idempotency_key`
	) VALUES (
		NEW.`device_id`,
		NEW.`project_slug`,
		NEW.`action`,
		NEW.`created_at`,
		'legacy-project-actions:' || NEW.`id`
	)
	ON CONFLICT (`device_id`, `idempotency_key`) DO NOTHING;
END;
--> statement-breakpoint
CREATE TRIGGER IF NOT EXISTS `project_action_events_reject_identity_replacement`
BEFORE INSERT ON `project_action_events`
WHEN EXISTS (
	SELECT 1
	FROM `project_action_events`
	WHERE `id` = NEW.`id`
		OR (`device_id` = NEW.`device_id` AND `idempotency_key` = NEW.`idempotency_key`)
)
BEGIN
	SELECT RAISE(ABORT, 'project_action_events identity is immutable');
END;
--> statement-breakpoint
CREATE TRIGGER IF NOT EXISTS `project_action_events_reject_update`
BEFORE UPDATE ON `project_action_events`
BEGIN
	SELECT RAISE(ABORT, 'project_action_events is append-only');
END;
--> statement-breakpoint
CREATE TRIGGER IF NOT EXISTS `project_action_events_reject_delete`
BEFORE DELETE ON `project_action_events`
BEGIN
	SELECT RAISE(ABORT, 'project_action_events is append-only');
END;
--> statement-breakpoint
INSERT INTO `project_action_events` (
	`device_id`, `project_slug`, `action`, `occurred_at`, `idempotency_key`
)
SELECT
	legacy.`device_id`,
	legacy.`project_slug`,
	legacy.`action`,
	legacy.`created_at`,
	'legacy-project-actions:' || legacy.`id`
FROM `project_actions` AS legacy
WHERE NOT EXISTS (
	SELECT 1
	FROM `project_action_events` AS existing
	WHERE existing.`device_id` = legacy.`device_id`
		AND existing.`project_slug` = legacy.`project_slug`
		AND existing.`action` = legacy.`action`
		AND ABS(julianday(existing.`occurred_at`) - julianday(legacy.`created_at`)) < (2.0 / 86400000.0)
)
ON CONFLICT (`device_id`, `idempotency_key`) DO NOTHING;
