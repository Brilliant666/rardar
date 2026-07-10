CREATE TABLE `project_actions` (
	`id` integer PRIMARY KEY AUTOINCREMENT NOT NULL,
	`device_id` text NOT NULL,
	`project_slug` text NOT NULL,
	`action` text NOT NULL,
	`created_at` text DEFAULT CURRENT_TIMESTAMP NOT NULL
);
--> statement-breakpoint
CREATE UNIQUE INDEX `project_actions_device_project_action_idx` ON `project_actions` (`device_id`,`project_slug`,`action`);--> statement-breakpoint
CREATE INDEX `project_actions_device_created_idx` ON `project_actions` (`device_id`,`created_at`);