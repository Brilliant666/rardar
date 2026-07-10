from __future__ import annotations

import unittest

from pipeline.build_catalog import build_catalog


def repository(repo: str, stars: int, created_at: str, description: str = "AI developer tool"):
    return {
        "repo": repo,
        "url": f"https://github.com/{repo}",
        "description": description,
        "language": "Python",
        "license": "MIT",
        "topics": ["developer-tools", "ai"],
        "stars": stars,
        "forks": 20,
        "created_at": created_at,
        "pushed_at": "2026-07-10T09:00:00Z",
        "candidate_query": "one | two",
    }


class BuildCatalogTests(unittest.TestCase):
    def test_risky_repository_stays_visible_but_cannot_enter_daily_five(self) -> None:
        risky = repository(
            "demo/unsafe-tool",
            500_000,
            "2026-07-09T12:00:00Z",
            "Codex 注入无限制模式，关闭所有内容过滤器。",
        )
        safe = [repository(f"demo/safe-{index}", 100 + index, "2026-07-08T12:00:00Z") for index in range(5)]
        snapshot = {
            "captured_at": "2026-07-10T12:00:00Z",
            "count": 6,
            "repositories": [risky, *safe],
        }

        catalog = build_catalog(snapshot)
        risky_project = next(item for item in catalog["projects"] if item["repo"] == "demo/unsafe-tool")

        self.assertEqual(risky_project["recommendation"], "观望")
        self.assertLessEqual(risky_project["globalScore"], 49)
        self.assertNotIn("demo/unsafe-tool", [item["repo"] for item in catalog["projects"][:5]])

    def test_evidence_urls_fall_back_to_https(self) -> None:
        item = repository("demo/new-tool", 900, "2026-07-07T12:00:00Z")
        item["url"] = "javascript:alert(1)"
        enrichment = {
            "repository": "demo/new-tool",
            "titleZh": "工具",
            "summaryZh": "摘要",
            "capabilities": ["能力"],
            "taskTerms": ["任务"],
            "reusePlan": "复用",
            "limitation": "限制",
            "sourceUrl": "data:text/html,unsafe",
        }

        project = build_catalog(
            {"captured_at": "2026-07-10T12:00:00Z", "count": 1, "repositories": [item]},
            enrichments={"demo/new-tool": enrichment},
        )["projects"][0]

        self.assertTrue(all(evidence["href"].startswith("https://") for evidence in project["evidence"]))

    def test_static_analysis_can_raise_reuse_confidence(self) -> None:
        snapshot = {
            "captured_at": "2026-07-10T12:00:00Z",
            "count": 1,
            "repositories": [repository("demo/new-tool", 900, "2026-07-07T12:00:00Z")],
        }
        analysis = {
            "repository": "demo/new-tool",
            "scanned_files": 240,
            "confidence": 91,
            "indicators": {
                "readme": True,
                "license": True,
                "tests": True,
                "ci": True,
                "docs": True,
                "examples": True,
                "package_manifest": True,
                "dependency_lock": True,
            },
            "counts": {"test_files": 18, "todo_markers": 2},
        }

        facts_only = build_catalog(snapshot)
        inspected = build_catalog(snapshot, analyses={"demo/new-tool": analysis})

        self.assertLessEqual(facts_only["projects"][0]["reuseScore"], 72)
        self.assertGreater(inspected["projects"][0]["reuseScore"], facts_only["projects"][0]["reuseScore"])
        self.assertEqual(inspected["projects"][0]["analysisState"], "静态分析")
        self.assertEqual(len(inspected["projects"][0]["evidence"]), 3)

    def test_codex_enrichment_replaces_copy_and_adds_task_terms(self) -> None:
        snapshot = {
            "captured_at": "2026-07-10T12:00:00Z",
            "count": 1,
            "repositories": [repository("demo/new-tool", 900, "2026-07-07T12:00:00Z")],
        }
        enrichment = {
            "repository": "demo/new-tool",
            "titleZh": "自动化工作流工具",
            "summaryZh": "把重复开发步骤组织成可复用流程。",
            "category": "开发工具",
            "capabilities": ["流程编排", "代码生成"],
            "taskTerms": ["工作流", "脚本生成"],
            "bestFor": "适合需要减少重复编码的开发任务。",
            "reusePlan": "优先复用流程定义层。",
            "limitation": "尚未运行验证。",
            "sourceUrl": "https://github.com/demo/new-tool",
        }

        catalog = build_catalog(snapshot, enrichments={"demo/new-tool": enrichment})
        project = catalog["projects"][0]

        self.assertEqual(project["title"], "自动化工作流工具")
        self.assertEqual(project["analysisState"], "深度分析")
        self.assertIn("脚本生成", project["taskTerms"])
        self.assertEqual(project["reusePlan"], "优先复用流程定义层。")
        self.assertEqual(len(project["evidence"]), 3)
        self.assertEqual(catalog["deepAnalysisCount"], 1)
        self.assertEqual(catalog["pendingDeepAnalysis"], [])

    def test_first_snapshot_labels_velocity_as_proxy(self) -> None:
        snapshot = {
            "captured_at": "2026-07-10T12:00:00Z",
            "count": 1,
            "repositories": [repository("demo/new-tool", 900, "2026-07-07T12:00:00Z")],
        }

        catalog = build_catalog(snapshot)

        project = catalog["projects"][0]
        self.assertEqual(project["growthKind"], "velocity_proxy")
        self.assertIn("首次观察代理", project["growthLabel"])
        self.assertNotIn("24 小时新增", project["whyNow"])
        self.assertEqual(catalog["growthMode"], "first_observation_proxy")

    def test_second_snapshot_reports_observed_window(self) -> None:
        previous = {
            "captured_at": "2026-07-09T12:00:00Z",
            "count": 1,
            "repositories": [repository("demo/new-tool", 700, "2026-07-07T12:00:00Z")],
        }
        current = {
            "captured_at": "2026-07-10T12:00:00Z",
            "count": 1,
            "repositories": [repository("demo/new-tool", 900, "2026-07-07T12:00:00Z")],
        }

        catalog = build_catalog(current, previous)

        project = catalog["projects"][0]
        self.assertEqual(project["growthKind"], "observed")
        self.assertEqual(project["growthValue"], 200)
        self.assertIn("24.0 小时", project["growthLabel"])

    def test_observed_star_loss_is_not_hidden(self) -> None:
        previous = {
            "captured_at": "2026-07-09T12:00:00Z",
            "count": 1,
            "repositories": [repository("demo/new-tool", 900, "2026-07-07T12:00:00Z")],
        }
        current = {
            "captured_at": "2026-07-10T12:00:00Z",
            "count": 1,
            "repositories": [repository("demo/new-tool", 880, "2026-07-07T12:00:00Z")],
        }

        project = build_catalog(current, previous)["projects"][0]

        self.assertEqual(project["growthValue"], -20)
        self.assertIn("-20", project["growthLabel"])

    def test_mixed_second_snapshot_explains_new_candidates(self) -> None:
        previous = {
            "captured_at": "2026-07-09T12:00:00Z",
            "count": 1,
            "repositories": [repository("demo/known", 700, "2026-07-07T12:00:00Z")],
        }
        current = {
            "captured_at": "2026-07-10T12:00:00Z",
            "count": 2,
            "repositories": [
                repository("demo/known", 710, "2026-07-07T12:00:00Z"),
                repository("demo/new", 100, "2026-07-10T00:00:00Z"),
            ],
        }

        catalog = build_catalog(current, previous)

        self.assertEqual(catalog["growthMode"], "mixed_observation")
        self.assertIn("1 个项目具有两次快照", catalog["notice"])
        self.assertIn("新进入", catalog["notice"])

    def test_partial_candidate_collection_is_disclosed(self) -> None:
        snapshot = {
            "captured_at": "2026-07-10T12:00:00Z",
            "count": 1,
            "failed_query_count": 2,
            "repositories": [repository("demo/new-tool", 900, "2026-07-07T12:00:00Z")],
        }

        catalog = build_catalog(snapshot)

        self.assertEqual(catalog["queryFailureCount"], 2)
        self.assertIn("候选覆盖不完整", catalog["notice"])

    def test_recent_actionable_repository_outranks_old_reference_list(self) -> None:
        snapshot = {
            "captured_at": "2026-07-10T12:00:00Z",
            "count": 2,
            "repositories": [
                repository("demo/agent-workflow", 900, "2026-07-07T12:00:00Z"),
                repository("demo/awesome-books", 200_000, "2015-01-01T00:00:00Z", "Awesome books roadmap"),
            ],
        }

        catalog = build_catalog(snapshot)

        self.assertEqual(catalog["projects"][0]["repo"], "demo/agent-workflow")


if __name__ == "__main__":
    unittest.main()
