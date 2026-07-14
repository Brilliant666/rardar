from __future__ import annotations

import unittest

from pipeline.build_catalog import _enrichment_is_current, build_catalog


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
    def test_enrichment_current_requires_exact_evidence_identity_and_valid_time(self) -> None:
        project_repo = "demo/new-tool"
        pushed_at = "2026-07-10T09:00:00Z"
        analysis = {
            "schemaVersion": 1,
            "repository": project_repo,
            "analyzed_at": "2026-07-10T10:00:00Z",
        }
        enrichment = {
            "schemaVersion": 1,
            "repository": project_repo,
            "sourcePushedAt": pushed_at,
            "sourceAnalysisAt": analysis["analyzed_at"],
            "analyzedAt": "2026-07-10T12:00:00Z",
        }

        self.assertTrue(
            _enrichment_is_current(enrichment, project_repo, pushed_at, analysis)
        )
        self.assertFalse(
            _enrichment_is_current(
                {**enrichment, "sourcePushedAt": "2026-07-10T08:00:00Z"},
                project_repo,
                pushed_at,
                analysis,
            )
        )
        self.assertFalse(
            _enrichment_is_current(
                {
                    **enrichment,
                    "sourcePushedAt": "2026-07-10T17:00:00+08:00",
                    "sourceAnalysisAt": "2026-07-10T18:00:00+08:00",
                },
                project_repo,
                pushed_at,
                analysis,
            ),
            "equivalent instants must not replace exact source strings",
        )
        self.assertFalse(
            _enrichment_is_current(
                enrichment,
                project_repo,
                pushed_at,
                {**analysis, "analyzed_at": "2026-07-10T11:00:00Z"},
            )
        )
        self.assertFalse(
            _enrichment_is_current(
                {**enrichment, "repository": "other/tool"},
                project_repo,
                pushed_at,
                analysis,
            )
        )
        self.assertFalse(
            _enrichment_is_current(
                {**enrichment, "analyzedAt": "2026-07-10T12:00:00"},
                project_repo,
                pushed_at,
                analysis,
            )
        )
        self.assertFalse(
            _enrichment_is_current(
                {**enrichment, "analyzedAt": "2026-07-10T09:59:59Z"},
                project_repo,
                pushed_at,
                analysis,
            ),
            "an enrichment cannot cite evidence produced after the enrichment",
        )

    def test_daily_five_balances_recent_momentum_and_long_term_heat(self) -> None:
        fast = [
            repository(f"demo/fast-{index}", 2_000 + index, "2026-07-01T00:00:00Z")
            for index in range(5)
        ]
        enduring = [
            repository("demo/enduring-one", 120_000, "2018-01-01T00:00:00Z"),
            repository("demo/enduring-two", 80_000, "2020-01-01T00:00:00Z"),
        ]

        catalog = build_catalog(
            {
                "captured_at": "2026-07-10T12:00:00Z",
                "count": len(fast) + len(enduring),
                "repositories": [*fast, *enduring],
            }
        )
        daily = catalog["projects"][:5]

        self.assertEqual(sum(item["heatTrack"] == "long_term" for item in daily), 2)
        self.assertEqual(sum(item["heatTrack"] == "recent_momentum" for item in daily), 3)
        self.assertEqual(catalog["dailyTrackCounts"], {"recentMomentum": 3, "longTerm": 2})
        self.assertTrue(all(item["enduranceScore"] >= 60 for item in daily if item["heatTrack"] == "long_term"))
        self.assertEqual(catalog["schemaVersion"], 2)
        self.assertEqual(catalog["scoreModelVersion"], "evidence-v2")
        self.assertTrue(all("globalScore" not in item for item in catalog["projects"]))
        self.assertTrue(all("reuseScore" not in item for item in catalog["projects"]))

    def test_long_term_heat_upgrades_after_persistent_snapshot_evidence(self) -> None:
        enduring = repository("demo/enduring", 120_000, "2018-01-01T00:00:00Z")
        history = [
            {
                "captured_at": f"2026-07-0{day}T12:00:00Z",
                "repositories": [enduring],
            }
            for day in range(4, 10)
        ]
        catalog = build_catalog(
            {
                "captured_at": "2026-07-10T12:00:00Z",
                "count": 1,
                "repositories": [enduring],
            },
            history=history,
        )
        project = catalog["projects"][0]

        self.assertEqual(project["heatTrack"], "long_term")
        self.assertEqual(project["longTermEvidenceKind"], "multi_snapshot")
        self.assertEqual(project["heatObservationCount"], 7)
        self.assertEqual(project["heatObservationWindow"], 7)
        self.assertIn("多周期验证", project["heatLabel"])
        self.assertEqual(catalog["heatHistory"]["verifiedLongTermCount"], 1)

    def test_sparse_history_stays_a_structural_proxy(self) -> None:
        enduring = repository("demo/enduring", 120_000, "2018-01-01T00:00:00Z")
        other = repository("demo/other", 80_000, "2019-01-01T00:00:00Z")
        history = [
            {
                "captured_at": f"2026-07-0{day}T12:00:00Z",
                "repositories": [enduring if day < 7 else other],
            }
            for day in range(4, 10)
        ]
        project = build_catalog(
            {
                "captured_at": "2026-07-10T12:00:00Z",
                "count": 1,
                "repositories": [enduring],
            },
            history=history,
        )["projects"][0]

        self.assertEqual(project["heatObservationCount"], 4)
        self.assertEqual(project["heatObservationWindow"], 7)
        self.assertEqual(project["longTermEvidenceKind"], "structural_proxy")

    def test_static_license_hint_is_disclosed_without_overstating_reuse_safety(self) -> None:
        item = repository("demo/license-hint", 900, "2026-07-07T12:00:00Z")
        item["license"] = None
        analysis = {
            "schemaVersion": 1,
            "repository": "demo/license-hint",
            "analyzed_at": "2026-07-10T10:00:00Z",
            "scanned_files": 100,
            "confidence": 90,
            "license_hint": "MIT",
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
            "counts": {"test_files": 12},
        }

        project = build_catalog(
            {"captured_at": "2026-07-10T12:00:00Z", "count": 1, "repositories": [item]},
            analyses={"demo/license-hint": analysis},
        )["projects"][0]

        self.assertEqual(project["license"], "MIT（静态线索）")
        self.assertIn("只读静态扫描识别", project["risk"])
        self.assertNotIn("尚未进行代码静态检查", project["risk"])
        self.assertIsInstance(project["engineeringReadiness"], int)
        self.assertNotEqual(project["recommendation"], "隔离试用")
        self.assertIsNone(project["reuseFitScore"])
        self.assertIn("没有任务上下文", project["scoreExplanations"]["reuseFit"]["limitations"][0])

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
        self.assertLessEqual(risky_project["attentionScore"], 49)
        self.assertNotIn("demo/unsafe-tool", [item["repo"] for item in catalog["projects"][:5]])

    def test_evidence_urls_fall_back_to_https(self) -> None:
        item = repository("demo/new-tool", 900, "2026-07-07T12:00:00Z")
        item["url"] = "http://["
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

    def test_static_analysis_establishes_engineering_readiness_without_reuse_fit(self) -> None:
        snapshot = {
            "captured_at": "2026-07-10T12:00:00Z",
            "count": 1,
            "repositories": [repository("demo/new-tool", 900, "2026-07-07T12:00:00Z")],
        }
        analysis = {
            "schemaVersion": 1,
            "repository": "demo/new-tool",
            "analyzed_at": "2026-07-10T10:00:00Z",
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

        self.assertIsNone(facts_only["projects"][0]["engineeringReadiness"])
        self.assertIsInstance(inspected["projects"][0]["engineeringReadiness"], int)
        self.assertGreater(inspected["projects"][0]["evidenceCompleteness"], facts_only["projects"][0]["evidenceCompleteness"])
        self.assertIsNone(inspected["projects"][0]["reuseFitScore"])
        self.assertEqual(inspected["projects"][0]["analysisState"], "静态分析")
        self.assertEqual(len(inspected["projects"][0]["evidence"]), 3)
        self.assertIn("不是运行可靠性", inspected["projects"][0]["scoreExplanations"]["engineeringReadiness"]["limitations"][0])

    def test_stale_static_analysis_cannot_raise_reuse_confidence(self) -> None:
        item = repository("demo/new-tool", 900, "2026-07-07T12:00:00Z")
        stale_analysis = {
            "schemaVersion": 1,
            "repository": "demo/new-tool",
            "analyzed_at": "2026-07-09T08:00:00Z",
            "scanned_files": 240,
            "confidence": 95,
            "indicators": {"readme": True, "license": True, "tests": True, "ci": True, "docs": True},
            "counts": {"test_files": 18},
        }

        project = build_catalog(
            {"captured_at": "2026-07-10T12:00:00Z", "count": 1, "repositories": [item]},
            analyses={"demo/new-tool": stale_analysis},
        )["projects"][0]

        self.assertEqual(project["analysisState"], "事实初筛")
        self.assertIsNone(project["engineeringReadiness"])
        self.assertIn("早于仓库最近推送", project["risk"])
        self.assertEqual(len(project["evidence"]), 2)

    def test_legacy_static_analysis_never_counts_as_current(self) -> None:
        item = repository("demo/new-tool", 900, "2026-07-07T12:00:00Z")
        legacy_analysis = {
            "schemaVersion": 0,
            "repository": "demo/new-tool",
            # A malformed legacy payload cannot become current merely by
            # carrying a recent-looking timestamp.
            "analyzed_at": "2026-07-11T10:00:00Z",
            "scanned_files": 240,
            "confidence": 95,
            "indicators": {"readme": True, "license": True, "tests": True},
            "counts": {"test_files": 18},
        }

        project = build_catalog(
            {"captured_at": "2026-07-10T12:00:00Z", "count": 1, "repositories": [item]},
            analyses={"demo/new-tool": legacy_analysis},
        )["projects"][0]

        self.assertEqual(project["analysisState"], "事实初筛")
        self.assertIsNone(project["engineeringReadiness"])

    def test_codex_enrichment_replaces_copy_and_adds_task_terms(self) -> None:
        snapshot = {
            "captured_at": "2026-07-10T12:00:00Z",
            "count": 1,
            "repositories": [repository("demo/new-tool", 900, "2026-07-07T12:00:00Z")],
        }
        enrichment = {
            "schemaVersion": 1,
            "repository": "demo/new-tool",
            "sourcePushedAt": "2026-07-10T09:00:00Z",
            "sourceAnalysisAt": "2026-07-10T10:00:00Z",
            "analyzedAt": "2026-07-10T10:00:00Z",
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
        analysis = {
            "schemaVersion": 1,
            "repository": "demo/new-tool",
            "analyzed_at": "2026-07-10T10:00:00Z",
            "indicators": {},
            "counts": {},
        }

        catalog = build_catalog(
            snapshot,
            analyses={"demo/new-tool": analysis},
            enrichments={"demo/new-tool": enrichment},
        )
        project = catalog["projects"][0]

        self.assertEqual(project["title"], "自动化工作流工具")
        self.assertEqual(project["analysisState"], "深度分析")
        self.assertIn("脚本生成", project["taskTerms"])
        self.assertEqual(project["reusePlan"], "优先复用流程定义层。")
        self.assertEqual(project["fitHypothesis"], "适合需要减少重复编码的开发任务。")
        self.assertIsNone(project["reuseFitScore"])
        self.assertEqual(
            project["scoreExplanations"]["reuseFit"]["score"],
            project["reuseFitScore"],
        )
        self.assertEqual(project["evidenceCompleteness"], 80)
        self.assertEqual(len(project["evidence"]), 4)
        self.assertEqual(catalog["deepAnalysisCount"], 1)
        self.assertEqual(catalog["pendingDeepAnalysis"], [])

    def test_score_explanations_match_every_published_score(self) -> None:
        project = build_catalog(
            {
                "captured_at": "2026-07-10T12:00:00Z",
                "count": 1,
                "repositories": [repository("demo/new-tool", 900, "2026-07-07T12:00:00Z")],
            }
        )["projects"][0]

        expected = {
            "attention": project["attentionScore"],
            "endurance": project["enduranceScore"],
            "engineeringReadiness": project["engineeringReadiness"],
            "reuseFit": project["reuseFitScore"],
            "evidenceCompleteness": project["evidenceCompleteness"],
        }
        self.assertEqual(set(project["scoreExplanations"]), set(expected))
        for name, score in expected.items():
            with self.subTest(name=name):
                explanation = project["scoreExplanations"][name]
                self.assertEqual(explanation["score"], score)
                self.assertTrue(explanation["summary"])
                self.assertEqual(
                    set(explanation),
                    {
                        "score",
                        "summary",
                        "facts",
                        "proxies",
                        "limitations",
                        "upgradeConditions",
                    },
                )

    def test_recommendation_never_claims_reuse_without_task_or_runtime_evidence(self) -> None:
        item = repository("demo/ready-tool", 8_000, "2026-07-01T00:00:00Z")
        analysis = {
            "schemaVersion": 1,
            "repository": "demo/ready-tool",
            "analyzed_at": "2026-07-10T10:00:00Z",
            "scanned_files": 300,
            "confidence": 95,
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
            "counts": {"test_files": 30},
        }
        project = build_catalog(
            {"captured_at": "2026-07-10T12:00:00Z", "count": 1, "repositories": [item]},
            analyses={"demo/ready-tool": analysis},
        )["projects"][0]

        self.assertEqual(project["recommendation"], "隔离试用")
        self.assertNotIn(project["recommendation"], {"复用", "试用"})
        self.assertIsNone(project["reuseFitScore"])
        self.assertIn("未执行测试", project["scoreExplanations"]["engineeringReadiness"]["limitations"][0])

    def test_repository_push_after_static_analysis_invalidates_later_enrichment(self) -> None:
        item = repository("demo/new-tool", 900, "2026-07-07T12:00:00Z")
        enrichment = {
            "schemaVersion": 1,
            "repository": "demo/new-tool",
            "sourcePushedAt": "2026-07-10T08:00:00Z",
            "sourceAnalysisAt": "2026-07-10T08:30:00Z",
            "analyzedAt": "2026-07-10T12:00:00Z",
            "titleZh": "旧画像",
            "summaryZh": "这份画像生成后仓库又更新了。",
            "capabilities": ["旧能力"],
            "taskTerms": ["旧任务"],
            "reusePlan": "重新核对后再复用。",
            "limitation": "尚未复核。",
        }
        analysis = {
            "schemaVersion": 1,
            "repository": "demo/new-tool",
            "analyzed_at": "2026-07-10T08:30:00Z",
            "indicators": {},
            "counts": {},
        }

        catalog = build_catalog(
            {"captured_at": "2026-07-10T12:00:00Z", "count": 1, "repositories": [item]},
            analyses={"demo/new-tool": analysis},
            enrichments={"demo/new-tool": enrichment},
        )
        project = catalog["projects"][0]

        self.assertEqual(project["analysisState"], "画像待复核")
        self.assertIn("缺少与仓库最新推送对应的只读静态证据", project["risk"])
        self.assertEqual(catalog["deepAnalysisCount"], 0)
        self.assertEqual(catalog["pendingDeepAnalysis"], ["demo/new-tool"])

    def test_new_static_analysis_invalidates_enrichment_bound_to_old_evidence(self) -> None:
        item = repository("demo/new-tool", 900, "2026-07-07T12:00:00Z")
        enrichment = {
            "schemaVersion": 1,
            "repository": "demo/new-tool",
            "sourcePushedAt": "2026-07-10T09:00:00Z",
            "sourceAnalysisAt": "2026-07-10T09:30:00Z",
            "analyzedAt": "2026-07-10T12:00:00Z",
            "titleZh": "绑定旧证据的画像",
            "summaryZh": "静态证据随后已更新。",
            "capabilities": ["旧能力"],
            "taskTerms": ["旧任务"],
            "reusePlan": "重新核对后再复用。",
            "limitation": "证据版本已更新。",
        }
        analysis = {
            "schemaVersion": 1,
            "repository": "demo/new-tool",
            "analyzed_at": "2026-07-10T10:00:00Z",
            "indicators": {},
            "counts": {},
        }

        catalog = build_catalog(
            {"captured_at": "2026-07-10T12:00:00Z", "count": 1, "repositories": [item]},
            analyses={"demo/new-tool": analysis},
            enrichments={"demo/new-tool": enrichment},
        )
        project = catalog["projects"][0]

        self.assertEqual(project["analysisState"], "画像待复核")
        self.assertIn("静态分析版本与当前证据不一致", project["risk"])
        self.assertEqual(catalog["deepAnalysisCount"], 0)

    def test_enrichment_without_static_evidence_is_not_applied(self) -> None:
        item = repository("demo/new-tool", 900, "2026-07-07T12:00:00Z")
        enrichment = {
            "schemaVersion": 1,
            "repository": "demo/new-tool",
            "sourcePushedAt": "2026-07-10T09:00:00Z",
            "sourceAnalysisAt": "2026-07-10T10:00:00Z",
            "analyzedAt": "2026-07-10T10:00:00Z",
            "titleZh": "不应采用的画像标题",
            "summaryZh": "缺少静态证据。",
            "capabilities": ["未验证能力"],
            "taskTerms": ["未验证任务"],
            "bestFor": "未验证场景",
            "reusePlan": "未验证方案",
            "limitation": "缺少静态证据",
            "evidenceSummary": "只有画像文件",
            "sourceUrl": "https://github.com/demo/new-tool",
        }

        project = build_catalog(
            {"captured_at": "2026-07-10T12:00:00Z", "count": 1, "repositories": [item]},
            enrichments={"demo/new-tool": enrichment},
        )["projects"][0]

        self.assertEqual(project["title"], "new-tool")
        self.assertEqual(project["analysisState"], "画像待复核")
        self.assertIn("缺少与仓库最新推送对应的只读静态证据", project["risk"])
        self.assertNotIn("未验证能力", project["capabilities"])
        self.assertEqual(len(project["evidence"]), 2)

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
