# Subscription-Aware Coding Agent Scheduler v0.3 仕様書

**Version:** 0.3
**Date:** 2026-08-12
**Status:** Draft / MVP Specification

---

## 0. Executive Summary

本システムは、GitHub Issueをタスクキューとして取得し、Claude CodeおよびCodex CLIをWorkerとして利用して、Issueを継続的に実装するローカルCoding Agent Schedulerである。

ユーザーは例えば、

```bash
subsched run \
  --repo owner/project \
  --issues all-open
```

または、

```bash
subsched run "GitHubのopen issueをすべて実行"
```

と一度指示する。

以降、

```text
GitHub Issues
      ↓
Task Queue
      ↓
Subscription Scheduler
      ↓
利用可能Agentを選択
   ┌──────┴──────┐
Claude Code     Codex
   │              │
   └──────┬───────┘
          ↓
Issue Worktree
          ↓
Verification
          ↓
PR
          ↓
Next Issue
```

という処理を、人間によるAgent切替なしで継続する。

Claude CodeまたはCodexが5時間枠・週次枠・一時的rate limit等で利用不能になった場合、Scheduler自身は停止せず、利用可能なAgentへタスクを切り替える。

最重要な責務分離は以下である。

```text
Scheduler
=
Queue / Capacity / State / Routing / Recovery

Claude Code / Codex
=
1 Issueを実装するWorker
```

**「GitHub Issueを全部実行」というトップレベル指示をClaude Codeに渡してはならない。**

---

# 1. Product Definition

## 1.1 定義

> **GitHub Issue QueueをClaude Code / Codexの利用可能なsubscription capacityに応じて継続的に消化するLocal Coding Agent Scheduler**

より一般化すると、

> **Capacity-aware Local Coding Agent Task Scheduler**

である。

---

# 2. 解決する問題

現在は例えば、

```text
Claude Code
   ↓
Issue #101
   ↓
Issue #102
   ↓
rate limit
   ↓
人間が確認
   ↓
Codexを起動
   ↓
状況説明
   ↓
Issue #102の続き
   ↓
Issue #103
```

となる。

問題はAIの実装能力ではなく、

```text
どのAgentが今使えるか
何を実行していたか
どこまで終わったか
次に何を実行すべきか
いつ利用可能に戻るか
```

のcoordinationを人間が担当していることである。

v0.3ではこれをSchedulerへ移す。

---

# 3. Target UX

## 3.1 基本操作

```bash
subsched run \
  --repo takurot/project \
  --label ai-ready
```

出力：

```text
22:00:00 Repository: takurot/project
22:00:01 GitHub issues discovered: 14

READY
#101 Fix timeout handling
#103 Add benchmark cache
#108 Refactor runner
#112 CSV export
...

Claude:
  5h      43%
  weekly  72%
  status  AVAILABLE

Codex:
  5h      78%
  status  AVAILABLE

22:00:03 Dispatch #101 -> Claude
```

GitHub CLIはIssueの一覧取得とJSON形式での出力をサポートしているため、MVPではGraphQL/RESTを直接実装せず `gh` をGitHub adapterとして利用できる。

---

# 4. Top-Level Prompt Ownership

トップレベル指示：

```text
GitHubのopen issueをすべて実行
```

を受け取るのは、

```text
Claude Code
```

ではなく、

```text
Subscription Scheduler
```

である。

禁止：

```bash
claude -p "GitHubのIssueを全部実行して"
```

推奨：

```bash
subsched run --issues all-open
```

SchedulerがIssueを1件ずつWorkerへ渡す。

---

# 5. Agent Responsibility

Claude Code / Codexが担当するのは常に、

```text
1 Agent invocation
=
原則 1 GitHub Issue
```

とする。

Agentへ以下を任せない。

```text
GitHub全Issueの管理
別Issueへの移動
Agent選択
capacity管理
retry timing
subscription reset管理
global concurrency
```

これらはSchedulerの責務とする。

---

# 6. Core Architecture

```text
                      User
                       │
            "open issueを全部実行"
                       │
                       ▼
        ┌──────────────────────────┐
        │ Subscription Scheduler   │
        │                          │
        │ GitHub Adapter           │
        │ Issue Queue              │
        │ Dependency Resolver      │
        │ Capacity Manager         │
        │ Agent Router             │
        │ Task State               │
        │ Verification Controller  │
        │ Recovery Manager         │
        └─────────────┬────────────┘
                      │
              ┌───────┴───────┐
              │               │
              ▼               ▼
        Claude Code        Codex CLI
              │               │
              └───────┬───────┘
                      ▼
                 Task Worktree
                      │
               .ai/handoff.md
                      │
                      ▼
                 Verification
                      │
                      ▼
                  GitHub PR
                      │
                      ▼
                   Next Issue
```

---

# 7. Technology Principles

以下を維持する。

```text
Native CLI
Native authentication
No credential proxy
No API fallback by default
No LLM in scheduling loop
Deterministic scheduler
Task-scoped worktree
Durable handoff
Provider capacity > local usage estimate
Verification > Agent self-report
```

---

# 8. OSS Strategy

## Required

```text
Git
GitHub CLI
Claude Code CLI
Codex CLI
Python 3.12+
```

## Candidate

```text
Bernstein
```

BernsteinはClaude CodeやCodexを含むCLI coding agent向けの決定論的Python orchestratorであり、taskごとのGit worktreeとlint/type/test gateを既に持つ。

ただしv0.3では**依存確定ではなくCompatibility Spike対象**とする。

## Telemetry

```text
ccusage
```

ccusageはClaude Code、Codexなど複数Coding Agent CLIのローカルusage dataを解析できる。

## Permanent Non-goals

```text
CLIProxyAPI
OAuth credential proxy
provider credential forwarding
automatic API fallback
```

---

# 9. GitHub Issue Discovery

MVP：

```bash
gh issue list \
  --state open \
  --limit 100 \
  --json number,title,body,labels,assignees,milestone,url
```

GitHub CLIの `gh issue list` はopen issue取得をサポートし、`--json` で構造化出力を取得できる。

---

# 10. Issue Selection Modes

三つ提供する。

## Safe

```bash
subsched run --label ai-ready
```

対象：

```text
ai-ready
```

ラベル付きIssueのみ。

---

## Explicit All

```bash
subsched run --issues all-open
```

すべてのeligible open issueを対象にする。

これはユーザーが明示的に指定した場合のみ許可する。

---

## Explicit Issue List

```bash
subsched run --issues 101,103,108
```

---

# 11. Eligibility Filter

Issueは以下を通過した場合のみREADYにする。

```text
OPEN
AND
not excluded
AND
not already in-progress
AND
not blocked
AND
not associated with active scheduler task
AND
not confirmed already implemented by a merged PR
```

最後の条件（#146）：discovery時に、対象Issueを実装した既存のmerged PRが無いかを`gh pr list
--search`で確認する。GitHub上のPR body/branch名はuntrusted schemaとして扱い、Schedulerが
自身の`create_or_get_pull_request()`で生成した規約（PR bodyが`Implements work for #N.`で
始まり、branchが`subsched/issue-N`）に厳密一致する場合のみ`CONFIRMED`とし、その場合は
Issueを新規READY taskとして一切discoveryに追加しない（重複実装を防ぐ）。issue番号への
言及はあるがこの規約に厳密一致しない場合（手動で作成したPRなど）や`gh`呼び出し自体が
失敗した場合は`AMBIGUOUS`としてfail closedにする：Issueはdiscoveryされるが`READY`では
なく`NEEDS_HUMAN`から開始し、理由を`Task.needs_human_reason`に記録する。

この確認は`subsched run --allow-rediscovery`で明示的に無効化できる（既定では無効化しない
= fail closedがdefault）。`--dry-run`でも同じ確認を行い、結果を discovery note として
出力する。

設定例：

```yaml
github:

  include_labels:
    - ai-ready

  exclude_labels:
    - wontfix
    - blocked
    - needs-design
    - human-only
    - security-sensitive
```

---

# 12. Issue Task Model

Issueを内部Taskへ変換する。

```json
{
  "task_id": "github-103",
  "issue": 103,
  "title": "Support timeout cancellation",
  "status": "READY",
  "attempt": 0,
  "current_agent": null,
  "worktree": null,
  "dependency": [],
  "pr": null
}
```

---

# 13. Task States

```text
DISCOVERED
    ↓
ELIGIBILITY_CHECK
    ↓
READY
    ↓
DISPATCHED
    ↓
IN_PROGRESS
    ↓
VERIFYING
   / \
PASS FAIL
 │     │
 ▼     ▼
PR_READY RETRY
 │
 ▼
READY_FOR_REVIEW
```

追加状態：

```text
BLOCKED
WAITING_CAPACITY
WAITING_DEPENDENCY
NEEDS_HUMAN
FAILED
CANCELLED
COMPLETE
```

---

# 14. Issue Queue

Schedulerは永続Queueを持つ。

```text
┌───────┬───────────────────────┬───────────────┐
│ Issue │ State                 │ Agent         │
├───────┼───────────────────────┼───────────────┤
│ #101  │ READY                 │ -             │
│ #103  │ IN_PROGRESS           │ Claude        │
│ #108  │ WAITING_CAPACITY      │ -             │
│ #112  │ WAITING_DEPENDENCY    │ -             │
│ #120  │ READY_FOR_REVIEW      │ Codex         │
└───────┴───────────────────────┴───────────────┘
```

Queue自体はLLMではなくPythonの決定論的ロジックで管理する。

---

# 15. Scheduling Loop

概念：

```python
while scheduler.running:
    sync_github()

    update_capacity()

    recover_interrupted_tasks()

    update_dependencies()

    for task in ready_tasks():
        agent = select_agent(task)

        if agent is None:
            task.state = WAITING_CAPACITY
            continue

        dispatch(task, agent)

    verify_finished_tasks()

    publish_completed_tasks()

    sleep_until_next_event()
```

---

# 16. Issue Priority

v0.3ではLLMによる優先順位判断を行わない。

例：

```yaml
queue:

  priority:
    - label: priority:critical
      score: 100

    - label: priority:high
      score: 80

    - label: bug
      score: 60

    - label: enhancement
      score: 40
```

同一score：

```text
Issue number ASC
```

等の決定論的tie-breakを使用する。

---

# 17. Capacity Model

Agent状態：

```text
AVAILABLE
BUSY

PRESSURED_SESSION
PRESSURED_WEEKLY

COOLDOWN_SESSION
COOLDOWN_WEEKLY
COOLDOWN_MODEL

RATE_LIMITED_TEMPORARY

AUTH_ERROR
DISABLED_BILLING
DISABLED

FAILED
UNKNOWN
```

---

# 18. Claude Capacity

Claude Codeは現在、Pro/Max subscriptionについてstatus line inputの `rate_limits` に、

```text
five_hour
seven_day
```

を提供し、それぞれ、

```text
used_percentage
resets_at
```

を含む。

またClaudeのPro/Maxには5時間のusage windowとweekly usage limitがあり、weekly reset時刻をUsage画面で確認できる。

したがって、

```text
cooldown = 5h
```

という固定値は持たない。

---

# 19. Codex Capacity

Codex subscription usageについてもlocal/cloud usageは5時間windowを共有し、追加weekly limitが適用される場合がある。

Scheduler Coreでは、

```text
Claude = 5h
Codex  = 5h
```

のようなProvider-specific値をハードコードしない。

Provider Adapterが、

```text
available
used
reset_at
scope
confidence
```

へ正規化する。

---

# 20. Unified Capacity Interface

```python
@dataclass
class Capacity:
    agent: str
    state: str
    scope: str | None

    used_percentage: float | None
    reset_at: datetime | None

    source: str
    observed_at: datetime

    confidence: str
```

例：

```json
{
  "agent": "claude",
  "state": "AVAILABLE",
  "scope": "five_hour",
  "used_percentage": 73.1,
  "reset_at": "2026-08-12T23:34:00+09:00",
  "source": "provider",
  "confidence": "high"
}
```

---

# 21. Sensor Priority

実装状態（Issue #165）: Claude/Codex CLIからprovider残量を取得する検証済みcommand/schemaは
まだ存在しない。したがってproduction CLIのsensorはpolicy上のenabled/billing状態だけを
`source="policy"`, `confidence="low"`として返す。provider出力parserはsanitized fixtureの
replay契約として凍結し、実probeがPhase 0で確認されるまでproduction admission controlや
cooldown解除には使わない。`source="provider"`, `confidence="high"`は実際に観測・検証した
payloadにだけ付与する。

```text
1 Provider-derived structured capacity
2 Structured CLI result/error
3 Explicit reset-time message
4 Exit code
5 stderr/stdout classification
6 ccusage local telemetry
7 Historical estimate
8 UNKNOWN
```

ccusageはローカルAgent dataを読むため、Provider側subscription全体の残量を表すground truthとしては使用しない。ClaudeのPro/Max usageはClaudeとClaude Codeで共有されるため、Claude Codeのローカル履歴だけでは観測できない消費経路が存在する。

---

# 22. Capacity-based Admission Control

Issue開始前：

```text
#103 READY
    ↓
capacity check
    ↓
Claude 94%
Codex 32%
    ↓
#103 -> Codex
```

ただしproactive routingは、

```text
fresh provider capacity
```

が得られている場合のみ行う。

ccusage等のローカル推定しかない場合は、Agentを「利用不能」とは判断しない。

---

# 23. Agent Selection

例：

```python
def select_agent(task):

    candidates = available_agents()

    if not candidates:
        return None

    return max(
        candidates,
        key=lambda a: (
            capacity_score(a),
            preference_score(a, task),
            historical_score(a, task),
        ),
    )
```

v0.3ではhistorical model selectionはoptional。

MVP：

```text
availability
+
remaining capacity
+
static priority
```

のみ。

---

# 24. Default Agent Preference

```yaml
agents:

  claude:
    priority: 100

  codex:
    priority: 90
```

これは設定可能。

---

# 25. Worker Prompt

Claudeへは例えば、

```text
You are implementing GitHub issue #103.

Read:
- AGENTS.md
- CLAUDE.md
- .ai/tasks/103.md
- .ai/handoffs/103.md

Work only on issue #103.

Use the existing task worktree.

Do not start another GitHub issue.

After each meaningful milestone:
update .ai/handoffs/103.md.

Before finishing:
run the verification commands defined for this repository.

Leave the worktree in a recoverable state.
```

を渡す。

Codexにも同等のinstructionを渡す。

---

# 26. Continuous Handoff

v0.3でもhandoffはfailure時にだけ生成しない。

```text
Task start
 ↓
handoff initialized
 ↓
Agent work
 ↓
meaningful milestone
 ↓
handoff update
 ↓
Agent work
 ↓
handoff update
```

`.ai/handoffs/103.md`：

```markdown
# Issue

#103 Support timeout cancellation

## Goal

Implement safe subprocess timeout handling.

## Current Plan

1. Implement SIGTERM
2. Wait grace period
3. SIGKILL
4. Add zombie-process test

## Completed

- Timeout path identified
- SIGTERM handling implemented

## Current Work

Implementing grace-period handling.

## Decisions

Do not modify the public Runner API.

## Known Broken State

test_timeout_cleanup currently fails.

## Next

Fix process reaping race.

## Last Checkpoint

2026-08-12T22:41:00+09:00
```

---

# 27. Agent Contract

以下を、

```text
AGENTS.md
CLAUDE.md
```

の両方へ記載する。

```text
After every meaningful milestone:

1. Update the task handoff.
2. Record completed work.
3. Record the current intent.
4. Record known broken state.
5. Record the next action.

Never begin another GitHub issue.

Do not delete Scheduler state files.

Run repository verification before declaring completion.
```

---

# 28. Mechanical Checkpoint

Agentとは別にScheduler自身が記録する。

```text
git status
git diff --stat
changed files
HEAD commit
test results
last Agent output
exit code
failure classification
timestamp
```

したがってhandoff：

```text
Semantic State
     +
Mechanical State
```

になる。

---

# 29. Worktree Strategy

原則：

> **1 Issue = 1 Worktree**

```text
repo
 │
 ├─ issue-101/
 │     └─ Claude
 │
 ├─ issue-103/
 │     ├─ Claude
 │     └─ Codex
 │
 └─ issue-108/
       └─ Codex
```

Agent単位ではworktreeを作らない。

Issue #103についてClaudeが途中まで編集した場合、

```text
issue-103 worktree
        ↓
そのままCodex
```

へ渡す。

---

# 30. Mid-Issue Failover

最重要フローの一つ。

```text
Issue #103
    │
    ▼
Claude
    │
    ├── implementation 70%
    │
    ├── handoff updated
    │
    └── SESSION_LIMIT
              │
              ▼
     classify capacity event
              │
              ▼
 Claude -> COOLDOWN_SESSION
              │
              ▼
   mechanical checkpoint
              │
              ▼
      select next agent
              │
              ▼
            Codex
              │
              ▼
    same issue worktree
              │
              ▼
   read handoff + git diff
              │
              ▼
       continue remaining work
```

commitはhandoffの必須条件としない。

---

# 31. Between-Issue Routing

Issue完了後はさらに単純である。

```text
#101 Claude COMPLETE
         ↓
capacity refresh
         ↓
Claude 98%
Codex 37%
         ↓
#102 -> Codex
```

これを、

> **Admission Control**

と呼ぶ。

実行中Agentを残量だけを理由に途中停止させるpreemptive migrationは行わない。

---

# 32. Structured Agent Results

Claude Codeは構造化outputを利用できるため、可能な限りmachine-readable resultを利用する。Claude CodeにはJSON等のstructured output機構が存在する。

classification：

```text
structured result
     ↓
structured error
     ↓
exit status
     ↓
text classification
```

---

# 33. Agent Execution Guard

Claude：

```text
max-turns
task runtime
Scheduler timeout
```

を組み合わせる。

Claude Agent SDKには最大turn数到達を構造的に検出する仕組みがある。

Scheduler側にも、

```yaml
execution:

  max_task_runtime: 6h
  max_agent_switches: 6
  max_same_agent_retries: 2
```

を持つ。

上記の「Scheduler timeout」は`execution.agent_timeout_seconds`（既定300秒、config可変）として実装されており、
`NativeWorker`が`claude`/`codex`を1回起動する際の`ProcessExecutionRequest.timeout_seconds`に渡される。これは
`max_task_runtime`（Task全体に対するガード）とは別の、単一のagent process呼び出しに対するタイムアウトである。

`execution.max_task_runtime`はTaskの初回dispatch時刻（`Task.run_started_at`、failover/retry/restartをまたいで保持され、
リトライのたびにリセットされない）からの経過時間に対する予算として`Scheduler`が毎`tick()`の先頭で強制する。予算超過時は
黙って打ち切る／再dispatchするのではなく、`NEEDS_HUMAN`へfail-closedでエスカレーションする（理由は
`Task.needs_human_reason`に`"execution.max_task_runtime exceeded (...)"`として記録され、`subsched status --verbose`の
`task runtime since:`行でdispatch起点も確認できる）。`max_task_runtime`が未設定（`None`）の場合はこのガードは無効。

---

# 34. Billing Safety

デフォルト：

```yaml
billing:

  api_fallback: false
  metered_usage: false
  unknown_mode: disable
```

Claudeではusage creditsを有効化するとsubscription limit到達後に標準API料金相当で継続できる仕組みがあるため、自動Schedulerでは追加課金への暗黙移行を許可しない。

状態：

```text
DISABLED_BILLING
```

を用意する。

`subsched run --allow-native`はbilling modeを推測しない。各enabled CLIがsubscription認証で、
metered/API fallbackが無効であることを独立に確認したoperatorが、実行ごとに
`--subscription-billing-verified`を明示しなければnative workerを起動しない。この確認は
capacity観測の代替ではなく、合成capacityのprovenanceをprovider/highへ昇格させない。

---

# 35. Provider Policy Risk

Schedulerは、

```text
claude -p = permanent subscription entitlement
```

を不変条件として扱わない。

Provider authentication/billing policyは外部依存条件である。

起動時に、

```text
authentication mode
billing mode
capacity visibility
```

をProvider Adapterが確認する。

不明：

```text
UNKNOWN_BILLING
        ↓
disable worker
```

をfail-closed defaultとする。

---

# 36. Dependency Handling

Issue依存関係：

```text
#101 Base API
 │
 ├── #102 CLI
 └── #103 UI
```

内部Task：

```text
#101 READY

#102 WAITING_DEPENDENCY
     blocked_by: #101

#103 WAITING_DEPENDENCY
     blocked_by: #101
```

#101完了：

```text
#101 PASS
    ↓
┌───┴───┐
#102   #103
READY  READY
```

MVPでは依存関係を以下から取得できるようにする。

```text
Scheduler config
Issue metadata
Issue body convention
```

例：

```text
Blocked-By: #101
```

LLM推論による依存関係生成はMVPでは行わない。

---

# 37. Concurrency

初期値：

```yaml
execution:
  concurrency: 1
```

Phase 2以降：

```yaml
execution:
  concurrency: 2
```

例：

```text
Task Queue
   │
   ├── #101 → Claude
   ├── #102 → Codex
   └── #103 → WAITING
```

同じIssueへClaudeとCodexを同時投入しない。

---

# 38. Conflict Prevention

複数Issueが同一ファイルを大規模変更する可能性がある。

v0.3ではSchedulerは原則、

```text
worktree isolation
+
separate branch
```

により作業衝突を防ぐ。

PR merge conflictは、

```text
NEEDS_REBASE
```

として別状態にする。

自動rebaseは後続Phase。

---

# 39. Completion Definition

Agent：

```text
Done
```

だけでは完了しない。

```text
Agent finished
      ↓
Verification
      ↓
tests
lint
typecheck
build
      ↓
PASS
```

で初めて、

```text
IMPLEMENTED
```

とする。

Bernsteinを採用できる場合、このverification gateを既存機能へ委譲する。Bernsteinはtaskごとのworktreeとlint/type/test gateを持つ。

## COMPLETEの定義（#142）

`COMPLETE`は「PRを作成した」ことを意味しない。PR作成直後の既定terminal stateは
`READY_FOR_REVIEW`であり、これはSchedulerローカルのverification（上記IMPLEMENTED）は
通過したが、CI結果・人間によるreviewはまだ得られていないことを示す。

- `execution.ci_monitoring: false`（既定）: `READY_FOR_REVIEW`のままとどまり、Scheduler
  は自動でCOMPLETEへ昇格させない。PR merge後の後始末（Issue再発見の抑止等）は#146の
  discovery-time reconciliationが別途担当する。
- `execution.ci_monitoring: true`: 各tickで`READY_FOR_REVIEW`かつ`pr`を持つTaskの
  CI状態を`gh pr checks`経由で確認する。
  - CI `PASS` → `COMPLETE`へ昇格する。
  - CI `FAIL` → `NEEDS_HUMAN`へ遷移する（失敗したcheck名を理由として記録）。**自動
    requeueはしない**。push/PR作成失敗（#128）やcommit message違反（#140）と同じ
    fail-closedの設計方針に合わせ、CI失敗は常に人間の判断を挟む。
  - CI `PENDING`/`UNKNOWN` → 状態を変更しない（`READY_FOR_REVIEW`のまま様子を見る）。

いずれの場合も、Issueの自動close・自動mergeは行わない（既存契約を維持）。

metricsは`issues_implemented`（`READY_FOR_REVIEW`/`PR_READY`/`COMPLETE`を含む「実装済み」
件数）、`issues_ready_for_review`（レビュー待ち件数）、`task_completion_rate`
（`COMPLETE`のみを分子とする完了率）を分離して報告する。

---

# 40. Pull Request Creation

設定：

```yaml
github:

  completion:
    create_pr: true
    close_issue: false
```

`create_pr: false`は、native実行・検証・ローカルcommitまでは通常通り行うが、rebase・branch
push・PR作成のいずれもSchedulerが呼ばない（`push_enabled=False`と同じ「GitHubへ一切書き込まない」
経路を通る）ことを意味する。Taskはローカルでのみ`COMPLETE`へ遷移し、`pr`フィールドは`null`の
ままとなる。branch pushだけを行いPRを作成しない、という中間状態は提供しない — GitHub上で
レビューできないpushされたbranchだけが残るのはoperatorにとって価値が無く、復旧困難な状態を
増やすだけであるため。

`close_issue: true`は現状サポートしない。close権限は独立したwrite permission tier（PR作成用
credentialとは別）を要求するSPEC変更を先に必要とするため、configロード時にfail closedで拒否
する。`close_issue: false`（既定）のみを許可する。

`subsched run`は起動時に、native実行・push・create_pr・close_issueの実効policyを1行で表示する。

GitHub CLIにはPR作成機能があり、PR bodyからIssueを関連付けられる。`Fixes #123` のような記述はmerge時にIssueをcloseする動作を持つため、MVPでは意図しない自動closeを避けるため本文テンプレートを制御する。

推奨PR body：

```markdown
Implements work for #103.

## Summary

...

## Verification

- pytest: PASS
- lint: PASS

Generated by Subscription Scheduler.

Issue is intentionally left open until review.
```

PR body側のsanitizeだけでは不十分である。Workerがローカルcommitへ`Fixes #N`/`Closes #N`/
`Resolves #N`（大小文字・活用形を問わない）を書いた場合、merge方式によってはPR body経由
せずGitHubの自動close挙動を誘発しうる。`Scheduler._finalize_verified_task()`はpush前に
`base_branch..HEAD`の新規commit messageを全文検査し、該当keywordを検出した場合はpush/PR
adapterを呼ばずNEEDS_HUMANへfail closedする（commit historyは書き換えない）。PR body
sanitizer（`_strip_close_keywords`）と同じ正規表現ポリシーを共有する。Worker prompt/
managed contract（AGENTS.md/CLAUDE.md）にも同じ禁止事項を明記する。

---

# 41. Final Issue State

PR作成後：

```text
READY_FOR_REVIEW
```

とする。

デフォルトではIssueを自動closeしない。

---

# 42. PR / CI Monitoring

optional：

```text
PR created
    ↓
CI
    ↓
PASS
```

GitHub CLIはPR checksの状態をstructured JSONで取得でき、pass/fail/pending等へ分類できる。

将来的には、

```text
CI FAIL
  ↓
same task requeue
  ↓
Claude/Codex
```

も可能。

v0.3 MVPのcritical pathには含めない。

---

# 43. Crash Recovery

taskごと：

```text
.ai/runtime/
   103.lock
   103.process.json
```

例：

```json
{
  "pid": 38122,
  "started_at": "2026-08-12T22:12:31+09:00",
  "agent": "claude",
  "issue": 103,
  "worktree": "/worktrees/issue-103"
}
```

起動時：

```text
read state
   ↓
PID exists?
   ↓
process start time matches?
   ↓
worktree valid?
   ↓
handoff valid?
   ↓
recover / resume
```

PIDだけで判断せず、起動時刻も照合してPID reuseを防ぐ。

---

# 44. Persistent Scheduler State

```text
.ai/
│
├── scheduler.yaml
├── scheduler.json
│
├── tasks/
│   ├── 101.json
│   ├── 103.json
│   └── 108.json
│
├── handoffs/
│   ├── 101.md
│   ├── 103.md
│   └── 108.md
│
├── runtime/
│
└── runs/
```

---

# 45. Waiting for Capacity

CLI実装では既存のone-shot互換性を維持し、継続待機は明示的な`subsched run --watch`で
有効化する。watchはpoll間隔（1〜300秒）とwall-clock上限（1〜86400秒）を必須境界として
持ち、上限到達時はdurable state/worktree/handoffを保持して終了する。CI PENDINGはpoll間隔
ごとに再確認する一方、capacity supplierは`wait_duration()`到来前に再実行しない。

例えば：

```text
Claude
WEEKLY_LIMIT
reset: Aug 16 13:00

Codex
SESSION_LIMIT
reset: Aug 12 23:42
```

なら、

```text
next_capacity_event
=
Codex 23:42
```

Schedulerは：

```text
WAITING_CAPACITY
```

へ移行する。

23:42：

```text
probe Codex
    ↓
available?
 ┌──┴──┐
yes   no
 │     │
run   update capacity
```

固定pollを高頻度で回さない。

---

# 46. No Agent Available

```text
Claude unavailable
Codex unavailable
```

でもSchedulerプロセスは終了しない。

上記は`--watch` opt-in時の意味論である。既定のone-shot `run`は待機状態と次回時刻を表示して
終了する。watch中のpauseは新規dispatchを止め、SIGINTはexit 130で安全に終了する。
provider/highのfresh probeが存在しない場合はpolicy/low観測でcooldownを解除せず、watch上限で
fail-closedに終了する。

```text
Task Queue
     ↓
WAITING_CAPACITY
     ↓
next reset
     ↓
resume
```

これが本製品の中核価値の一つ。

---

# 47. Full Execution Example

ユーザー：

```bash
subsched run \
  --repo takurot/project \
  --label ai-ready
```

Scheduler：

```text
22:00:00 Repository synchronized
22:00:01 14 eligible issues

22:00:02 Capacity snapshot

Claude
  5h      43%
  weekly  72%
  status  AVAILABLE

Codex
  5h      78%
  status  AVAILABLE
```

Dispatch：

```text
22:00:03 #101 -> Claude
```

Claude：

```text
22:03 milestone checkpoint
22:12 milestone checkpoint
22:18 implementation finished
```

Verification：

```text
22:18 pytest PASS
22:18 lint PASS
22:19 PR #301 created

#101 -> READY_FOR_REVIEW
```

次：

```text
22:19 #103 -> Claude
```

途中：

```text
22:32 handoff updated
22:46 handoff updated

22:51 Claude SESSION_LIMIT
```

Scheduler：

```text
22:51:01 classify=CAPACITY_SESSION
22:51:01 Claude -> COOLDOWN_SESSION
22:51:01 reset_at=23:34

22:51:02 mechanical checkpoint
22:51:02 #103 remains IN_PROGRESS

22:51:03 Codex AVAILABLE
22:51:03 #103 -> Codex
```

Codex：

```text
22:51 read issue
22:51 read handoff
22:52 inspect git diff

22:55 continue timeout implementation

23:07 pytest PASS
23:08 lint PASS
23:08 PR #302 created
```

次：

```text
23:08 #108 -> Codex
23:27 #108 PASS
23:28 PR #303
```

その後：

```text
23:31 Codex WEEKLY_LIMIT
```

Scheduler：

```text
Codex -> COOLDOWN_WEEKLY
reset_at = Aug 15 09:00

Claude:
reset_at = 23:34

Task queue:
#112 READY
#120 READY
#126 READY

Scheduler -> WAITING_CAPACITY
```

23:34：

```text
Claude capacity probe
      ↓
AVAILABLE
      ↓
#112 -> Claude
```

そのままQueueを消化する。

---

# 48. Long-Running Example

例えば17 Issueある場合：

```text
22:00 #101 Claude
22:19 #101 PASS

22:20 #103 Claude
22:51 Claude limit

22:51 #103 Codex
23:08 #103 PASS

23:09 #108 Codex
23:28 #108 PASS

23:31 Codex weekly limit

23:34 Claude restored

23:35 #112 Claude
00:02 #112 PASS

00:03 #120 Claude
00:27 #120 PASS

...
```

ユーザーはAgentを切り替えない。

---

# 49. Human Intervention

以下だけ停止候補。

```text
ambiguous requirements
destructive migration
security-sensitive change
unresolvable merge conflict
push failure
PR creation failure
test environment unavailable
repeated Agent failures
billing mode unknown
authentication failure
max retries exceeded
```

状態：

```text
NEEDS_HUMAN
```

`Task.needs_human_reason`に、遷移理由を表す人間可読な文字列（redact済み）を永続化する。
`subsched status --verbose`で確認できる。理由が無い場合は`null`。

## push / rebase / PR作成失敗のリトライ方針

`Scheduler._finalize_verified_task()`内のrebase conflict、push失敗、PR作成失敗は、
Agent自体の失敗（`per_agent_failures`が`max_agent_failures`に達するまでリトライされる）
とは異なり、**リトライせず1回の失敗で即座にNEEDS_HUMANへ遷移する**。これは意図的な
挙動である。push/rebase/PR作成はGitHub上の状態を書き換える操作であり、原因不明のまま
自動リトライすると、non-fast-forward push、重複PR、意図しないrebaseなど、単純な
再試行よりも回復困難な状態を招くリスクがある。「不明な場合はfail-closedにする」という
本SPECの方針（§1）に従い、これらの失敗は都度人間の判断を挟む。

---

# 50. Loop Guard

```yaml
safety:

  max_agent_switches_per_task: 6

  max_agent_failures:
    claude: 2
    codex: 2

  max_task_runtime: 6h

  max_total_tasks_per_run: 50
```

capacity failoverはAgent failureとは別にカウントする。

`max_agent_failures`はTaskごと・Agentごとの`per_agent_failures[agent]`にのみ適用され、
`Task.attempt`（verification retryを含むTask全体のretry回数）は参照しない。verification
gateの失敗は`per_agent_failures`を増やさず、代わりに独立したカウンタ`verification_failures`
と設定値`execution.max_verification_failures`（default 2）で管理する。Agent failureと
verification failureのbudgetを混在させないのは、同じAgentが初回失敗しただけで
verification retry歴のせいで`NEEDS_HUMAN`へ誤昇格させないため（#175）。
crash recoveryも同じ契約に従い、復旧対象のdispatch Agentについて
`per_agent_failures[agent]`を増やして判定する。永続stateからAgentを一意に特定できない場合は、
誤ったfailure countを更新せず`NEEDS_HUMAN`へfail-closedで遷移する。

---

# 51. Metrics

v0.3では「Agent Switch数が少ないほどよい」としない。

## Capacity Metrics

```text
capacity_exhaustion_events
capacity_failover_count
capacity_failover_success_rate
waiting_for_capacity_time
```

主要指標：

```text
Capacity Failover Success Rate

=
successfully continued capacity events
/
all capacity failover events
```

MVP目標：

```text
> 95%
```

---

# 52. Reliability Metrics

```text
task_completion_rate

handoff_recovery_success_rate

crash_recovery_success_rate

agent_failure_switch_rate

manual_intervention_rate
```

---

# 53. Productivity Metrics

```text
issues_attempted
issues_implemented
PRs_created
issues_per_day
active_agent_time
capacity_wait_time
```

特に：

```text
Autonomous Issue Completion Rate

=
READY_FOR_REVIEW
/
eligible issues attempted
```

を重要指標とする。

---

# 54. Scheduler Configuration

```yaml
github:

  repo: takurot/project

  mode: label

  include_labels:
    - ai-ready

  exclude_labels:
    - blocked
    - human-only
    - security-sensitive

  completion:
    create_pr: true
    close_issue: false


agents:

  claude:
    enabled: true
    priority: 100

  codex:
    enabled: true
    priority: 90


routing:

  strategy: capacity-aware

  provider_capacity:
    preferred: true

  local_estimate:
    proactive_switch: false


billing:

  api_fallback: false
  metered_usage: false
  unknown_mode: disable


execution:

  concurrency: 1

  max_agent_switches: 6
  max_task_runtime: 6h
  max_tasks_per_run: 50
  agent_timeout_seconds: 300


handoff:

  continuous: true


verification:

  commands:
    - pytest
    - ruff check .
```

---

# 55. CLI

基本：

```bash
subsched run --repo owner/project --label ai-ready
```

全件：

```bash
subsched run --repo owner/project --issues all-open
```

一部：

```bash
subsched run --repo owner/project --issues 101,103,108
```

状態：

```bash
subsched status
```

例：

```text
Scheduler RUNNING

Queue
READY              7
IN_PROGRESS        1
WAITING_CAPACITY   0
READY_FOR_REVIEW   4
NEEDS_HUMAN        1

Agents

Claude
AVAILABLE
5h: 53%
weekly: 76%

Codex
BUSY
task: #108
```

---

# 56. Pause / Resume

```bash
subsched pause
```

意味：

```text
new issue dispatchを停止
```

現在実行中Taskは`execution.pause_running_policy`設定次第。3値の意味と実装状態：

| 値 | 意味 | 実装状態 |
|---|---|---|
| `continue` | 実行中Taskはそのまま継続させ、`pause`は新規dispatchのみ止める | 実装済み・既定値 |
| `abort` | 実行中Taskのprocess groupを強制終了する | **未実装**。process group制御・Task状態遷移が存在しないため、config読み込み時に`ConfigError`でfail-fast拒否される |
| `cancel` | 実行中Taskを`CANCELLED`へ遷移させつつ穏当に停止させる | **未実装**。同上の理由でfail-fast拒否される |

`abort`/`cancel`を黙って受理して実際には何もしない（fail-open）動作は許容しない。実装されるまでは
`continue`のみをconfigとして受理する。

```bash
subsched resume
```

でQueueを再開。

---

# 57. Cancel

```bash
subsched cancel 103
```

Issue #103だけ停止。

worktreeは削除せず保持する。

---

# 58. Bernstein Compatibility Spike

Bernsteinはtaskごとのworktree、複数CLI agent、verification gateをすでに提供しているため、再実装を避けられる可能性が高い。

ただし以下を実測する。

### B1 Same Worktree

```text
Claude
 ↓
failure
 ↓
Codex
```

を同じtask worktreeで実行できるか。

### B2 Dirty Worktree

```text
uncommitted changes
      ↓
Agent switch
```

を許容できるか。

### B3 Capacity Failure Semantics

```text
subscription limit
```

を、

```text
task failed
```

ではなく、

```text
provider unavailable
```

として再routingできるか。

### B4 Verification Reuse

Claude → Codex切替後にも同一verification pipelineを適用できるか。

---

# 59. Bernstein Adoption Decision

全部PASS：

```text
Subscription Scheduler
        ↓
Bernstein
        ↓
Claude / Codex
```

一つ以上critical failure：

```text
Subscription Scheduler
        ↓
Custom Sequential Runner
        ↓
Git Worktree
        ↓
Claude / Codex
```

Bernstein forkをMVP前提にはしない。

### Spike評価結果と決定 (Issue #8 / Task A5)

- **BS1 (Same Worktree):** Custom Git worktree adapterで同一worktreeを維持可能。Bernstein単体では異種Agent間でのworktreeとsemantic handoffの継続が未対応。
- **BS2 (Dirty Worktree):** BernsteinはAgent終了時にクリーンアップを行う前提があり、uncommitted diffを保持したまま別Agentへ安全に引き継ぐ要件を満たさない (FAIL)。
- **BS3 (Capacity Failure Semantics):** Bernsteinはcapacity/rate limitによるprocess exitをtask failureと判定するため、provider unavailableとしてqueue先頭へ戻す要件を満たさない (FAIL)。
- **BS4 (Verification Reuse):** failover後も同一のverification runnerを適用可能だが、Scheduler本体のライフサイクル管理下で動かす方が安全。

**決定: DEFER**
- Phase 1 / MVPではBernsteinをruntime dependencyに追加せず、Custom Git Worktree adapter (`subsched.tasks.worktree`) を採用する。
- 再評価期限: Phase 2 Native execution安定後、またはBernstein側でcapacity-aware routingおよびdirty worktree保持がサポートされた時点。

---

# 60. Implementation Structure

```text
subsched/
│
├── cli.py
├── scheduler.py
├── router.py
├── state.py
│
├── github/
│   ├── issues.py
│   └── pull_requests.py
│
├── agents/
│   ├── base.py
│   ├── claude.py
│   └── codex.py
│
├── capacity/
│   ├── base.py
│   ├── claude.py
│   ├── codex.py
│   └── ccusage.py
│
├── tasks/
│   ├── queue.py
│   ├── dependency.py
│   └── worktree.py
│
├── handoff/
│   ├── semantic.py
│   └── mechanical.py
│
├── verification/
│   └── runner.py
│
└── recovery/
    ├── lock.py
    └── process.py
```

---

# 61. Core Scheduler Pseudocode

```python
while running:
    github.sync()

    capacity.refresh()

    recovery.reconcile()

    tasks.refresh_dependencies()

    completed = workers.collect_finished()

    for result in completed:
        if result.capacity_exhausted:
            capacity.mark_unavailable(
                result.agent,
                result.reset_at,
                result.limit_scope,
            )

            handoff.checkpoint(result.task)

            task_queue.requeue_front(result.task)

            continue

        if result.agent_failed:
            task_queue.retry(result.task)
            continue

        task_queue.mark_verifying(result.task)

    verifier.process()

    publisher.create_ready_prs()

    while workers.have_capacity():
        task = task_queue.next_ready()

        if task is None:
            break

        agent = router.select(task, capacity)

        if agent is None:
            task.state = WAITING_CAPACITY
            break

        workers.dispatch(
            task=task,
            agent=agent,
            worktree=task.worktree,
        )

    scheduler.wait_for_next_event()
```

---

# 62. Important Invariant

常に以下を満たす。

```text
Issue State
+
Worktree State
+
Handoff State
+
Agent Process State
```

から、

> Schedulerを再起動しても現在作業を復元できる

こと。

Agent conversationは復元に必要なstateではない。

---

# 63. Phase 0 — Assumption Validation

実装前：

```text
Claude native headless execution
Claude structured failure
Claude capacity visibility

Codex native execution
Codex capacity failure behavior

GitHub CLI JSON

Bernstein same-worktree failover
```

を実測する。

結果を、

```text
.assumptions/
```

へ記録する。

---

# 64. Phase 1 — Queue MVP

実Agentは使わない。

Mock：

```text
GitHub Issue Queue
       ↓
Mock Claude
       ↓
Mock Codex
```

テスト：

```text
#101 -> Claude -> PASS
#102 -> Claude -> SESSION_LIMIT
#102 -> Codex -> PASS
#103 -> Codex -> WEEKLY_LIMIT
#103 -> WAITING
Claude reset
#103 -> Claude -> PASS
```

---

# 65. Phase 1 Acceptance Criteria

* GitHub IssueをTask Queueへ変換できる
* 1 Issueずつdispatchできる
* capacity eventでAgentを切替できる
* Issue途中のworktreeを維持できる
* Schedulerがlimitによって終了しない
* 両Agent unavailable時にWAITできる
* reset後に自動再開できる

---

# 66. Phase 2 — Native Agent MVP

```text
Claude Code
Codex CLI
```

を接続。

対象はテスト用Repositoryの3〜5 Issue。

---

# 67. Phase 3 — Durable Handoff

追加：

```text
continuous handoff
mechanical checkpoint
PID lock
crash recovery
```

Schedulerを強制killして再起動するテストを行う。

---

# 68. Phase 4 — GitHub PR Loop

追加：

```text
verification
branch push
PR creation
READY_FOR_REVIEW
```

GitHub CLIはPR作成、一覧、状態確認を提供している。

---

# 69. Phase 5 — Capacity Intelligence

追加：

```text
Claude provider capacity
Codex provider capacity
ccusage secondary telemetry
reset-aware scheduling
```

Claude Codeのstatus lineではsubscriptionの5時間/7日windowについて使用率とreset epochを取得できるため、この情報をProvider Adapterへ正規化する。

---

# 70. Phase 6 — Parallel Scheduling

```yaml
execution:
  concurrency: 2
```

へ拡張。

```text
Claude -> #101
Codex  -> #102
```

を並行実行。

---

# 71. Non-goals v0.3

以下は対象外。

```text
LLM-based global scheduler

Conversation migration

Provider OAuth proxy

API fallback

Automatic issue close

Automatic production deployment

Unlimited autonomous execution

Automatic merge to main

Automatic security-sensitive changes

Cross-machine distributed scheduler
```

---

# 72. Security Boundary

自動AgentにはRepository内でコードを実行する権限があるため、

```text
environment filtering
secret exclusion
worktree isolation
command logging
GitHub permission minimization
```

を必要とする。

特に、

```text
push
PR creation
issue mutation
```

と、

```text
merge
release
production deploy
```

を別permission tierとして扱う。

MVPは前者まで。

## 72.1 既知の制約: OSレベルsandboxは未実装

Native worker（`NativeWorker`）は、Claude Code CLIを`--permission-mode bypassPermissions`で起動する。これはtask worktree内での自律実行を機能させるために必要（`--print`非対話モードでは、`bypassPermissions`以外の承認モードは確認できる人間が存在しないため全アクションを拒否してしまい、実質何もできない）だが、以下を理解した上で運用すること。

- worktreeはcwdの既定値に過ぎず、コンテナ・chroot・ネットワーク制限等のOSレベルsandboxではない。`bypassPermissions`下のBashツールは、technicalにはworktree外のファイル読み書きや外部ネットワークアクセスを妨げられない。
- PRレビューは、エージェントが提案するコード差分（コミット内容）のみを検証する。セッション中に実行されたBashコマンドの副作用（worktree外のファイル変更、データの持ち出し等）はPRレビューの対象に含まれない。
- 環境変数は`COMMON_ENV_ALLOWLIST`でフィルタしてから子プロセスへ渡すため、secretの環境変数経由での露出は防いでいるが、ファイルシステム上のsecret（例: `~/.ssh`）へのアクセス自体は制限していない。

したがって現状のMVPは、**信頼できるIssue・信頼できるリポジトリでのみ**使用することを前提とする。真のOSレベルsandbox（コンテナ実行、ファイルシステム・ネットワークの隔離）は本Security Boundaryが要求する将来の強化項目であり、Phase 2実装時点では未達成である。

---

# 73. Success Criteria

最終的なv0.3 MVP成功条件：

ユーザーが一度、

```bash
subsched run \
  --repo owner/project \
  --label ai-ready
```

を実行した後、

```text
Issue discovery
    ↓
Implementation
    ↓
Agent rate limit
    ↓
Agent switch
    ↓
Implementation continuation
    ↓
Verification
    ↓
PR creation
    ↓
Next Issue
```

が、

```text
manual Agent switching = 0
```

で継続すること。

---

# 74. North-Star Scenario

最終的に実現したい体験は以下である。

```text
夜 22:00

$ subsched run --issues all-open

17 issues discovered.

Claude available.
Codex available.

Starting...
```

翌朝：

```text
Subscription Scheduler Report

Issues discovered          17

READY_FOR_REVIEW           11
NEEDS_HUMAN                 2
WAITING_DEPENDENCY          2
FAILED                      0
NOT_STARTED                 2

Pull Requests created      11

Capacity events

Claude session limit        2
Claude weekly limit         0

Codex session limit         1
Codex weekly limit          1

Successful agent failovers  4 / 4

Manual agent switches       0
```

ユーザーがやるのは、

```text
11 PRをレビューする
2 NEEDS_HUMANを見る
```

だけである。

---

# 75. v0.3の本質

v0.1：

```text
Claude limit
   ↓
Codex
```

v0.2：

```text
Provider Capacity
      +
Durable Task State
      ↓
Agent Scheduling
```

v0.3：

```text
                GitHub Issue Queue
                       │
                       ▼
               Durable Task State
                       +
               Provider Capacity
                       │
                       ▼
          Deterministic Task Scheduler
                /               \
          Claude Code          Codex
                \               /
                 Task Worktrees
                       │
                       ▼
                  Verification
                       │
                       ▼
                       PR
                       │
                       ▼
                    Next Issue
```

となる。

したがって、本システムの中心価値は、

> **Claude CodeとCodexを切り替えること**

ではない。

> **AI Coding Agentのsubscription capacityを計算資源として扱い、GitHub上のSoftware Engineering Task Queueを、人間のAgent運用なしに継続的に実行すること**

である。

これをv0.3の正式なProduct Definitionとする。
