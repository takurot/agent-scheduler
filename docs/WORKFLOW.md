# Subscription Agent Scheduler 開発ワークフロー

**最終更新:** 2026-08-13
**対象:** `subscription-agent-scheduler` v0.3仕様 / Phase 1実装

この文書は、`subsched` のGitHub Issueを安全に実装し、仕様、コード、テスト、運用状態の
ずれを防ぐための手順を定義する。現在の実装はPhase 1 Queue MVPであり、Claude Code /
Codexのnative実行、branch push、Pull Request作成はまだ有効化しない。

## 1. Source of truth

作業前に次の順で確認する。

1. [SPEC](SPEC.md) — v0.3の機能、安全性、不変条件
2. [GitHub Issues](https://github.com/takurot/agent-scheduler/issues) — タスク、優先度、依存関係、受け入れ条件
3. [README](../README.md) — 現在利用できる機能と利用者向け手順
4. 実装とテスト — 現在の挙動と保護されている契約

`docs/PLAN.md`はIssue移行前のローカル計画であり、GitHub Issueとの二重管理を避けるため
Git管理しない。新しい作業はIssueへ記録し、PLANを更新しない。

IssueとSPECが矛盾する場合、実装者の判断だけでどちらかを変更しない。SPEC変更を先に
提案・承認し、同じ変更でIssue、README、設定例、テストを同期する。特に次の契約は、
SPEC変更なしに緩和しない。

- subscriptionのみを使用し、API fallbackやmetered usageを有効化しない
- 1 Issue = 1 Task = 1 branch = 1 worktree
- capacity eventをAgent failureとして数えない
- Agent切替後も同じworktree、semantic handoff、mechanical checkpointを使う
- verification成功前にPRを作成しない
- Issueを自動closeせず、merge、release、production deployを自動化しない
- billing、auth、capacity、schemaが不明な場合はfail-closedにする

## 2. Issueを選ぶ

Issueを一覧し、本文、依存関係、コメントを確認する。

```bash
gh issue list --repo takurot/agent-scheduler --state open --limit 1000
gh issue view <ISSUE> \
  --repo takurot/agent-scheduler \
  --json number,title,body,comments,labels,state,url
```

一覧が指定上限へ到達した場合は全件取得できたと仮定せず、検索条件の分割またはpagination
で欠落がないことを確認する。`<ISSUE>`には確認済みの正整数だけを使い、Issue本文やtitle
などの外部入力をshell commandへ貼り付けない。

原則として、Issue本文の`Task ID`と依存Issueを確認し、依存作業が完了してから開始する。
P0はnative workerを有効化する前の安全ゲート、P1はv0.3 MVPのcritical path、P2は運用・
並列化、P3はMVP外の拡張である。

開始前に次をIssueコメントまたは作業メモへ整理する。

- 対応するSPEC節と受け入れ条件
- 変更対象と非スコープ
- 先に追加する失敗テスト
- state schema、CLI、GitHub、worktreeへの影響
- billing、secret、process、filesystem、復旧上のリスク
- ローカルで検証できないlive条件

Issueが大きすぎる場合は、受け入れ条件を失わない単位へ分割する。元Issueには子Issueと
完了条件を記録する。別Issueの作業を同じbranchへ混ぜない。

## 3. 作業開始

### 3.1 mainを同期する

既存の変更を破棄せず、mainをfast-forwardで同期する。

```bash
git switch main
git pull --ff-only
git status --short --branch
```

未追跡ファイルや別作業の差分がある場合は、所有者の変更として保持する。`git reset
--hard`、広い`git clean`、無関係なファイルへの`git checkout --`は使用しない。

### 3.2 Issue branchを作る

```bash
git switch -c issue/<ISSUE>-<short-description>
```

1 branchには1 Issueだけを含める。Schedulerが管理するtask branch名
`subsched/issue-N`はnative実行用の契約なので、通常の開発branchと混同しない。
`<ISSUE>`は正整数、`<short-description>`は小文字ASCII、数字、hyphenだけで構成し、
branch名全体を短く保つ。

### 3.3 環境を準備する

```bash
uv sync --frozen --extra dev
```

Pythonは3.12以上を使用する。依存関係を変更するIssue以外では`uv.lock`を意図せず更新
しない。`uv run subsched doctor`はnative phase用CLIを含む環境診断であり、現状はGit、gh、
Claude、Codexのいずれかがなければnonzeroになる。Phase 1のscripted worker開発ではsetup
gateにせず、native adapterを扱うIssueで診断結果を確認する。doctorはtokenやcapacityを
消費するlive probeの代わりにはしない。

## 4. TDD実装サイクル

すべての機能追加、修正、リファクタリングはRed → Green → Refactorで進める。

### 4.1 Red

受け入れ条件を観測できる最小のテストを先に追加し、意図した理由で失敗することを確認
する。テスト層は次のように使い分ける。

| 層 | 対象 | 原則 |
| --- | --- | --- |
| Unit | model、queue、router、config、状態遷移 | 外部process、実時間、filesystemに依存しない |
| Integration | scheduler、storage、GitHub adapter、worktree、recovery | temporary directory/repositoryとfake adapterを使う |
| E2E | CLIのrun/status/pause/resume/cancel、主要failover | 現在はTyper `CliRunner`とisolated stateを使い、providerは呼ばない |

Schedulerテストでは実時間の`sleep`、実GitHub書き込み、実Claude/Codexを使わない。
FakeClock、Scripted Worker、fixture、temporary Git repositoryを注入し、次を決定論的に
検証する。

- priority降順、同点はIssue番号昇順
- concurrency=1で同じIssueを二重dispatchしない
- capacity event後に同じTaskをqueue先頭へ戻し、worktreeを維持する
- 全Agent unavailable時に終了やbusy pollingをせず、最早resetを待つ
- reset後もfresh provider probeなしにworkerを有効化しない
- pause中は実行中Taskを継続し、新規dispatchを止める
- restart後もqueue、cooldown、counter、run上限を復元する
- corrupt/unknown state、billing/auth不明、security-sensitive Issueをfail-closedにする

### 4.2 Green

失敗テストを通す最小の実装を追加する。外部境界はinterfaceの内側へ閉じ込める。

| 領域 | 現在の主な場所 | 契約 |
| --- | --- | --- |
| Domain | `models.py`, `queue.py`, `router.py` | immutable object、明示的state transition、決定論的routing |
| Scheduler | `scheduler.py` | 1回に1 Task、durable state、capacity-aware wait |
| Persistence | `storage.py` | schema検証、atomic write、fail-closed load |
| Configuration | `config.py`, `examples/scheduler.yaml` | strict validation、安全なdefault、unknown key拒否 |
| GitHub read | `github/issues.py` | argv実行、入力schema検証、secret非表示 |
| CLI | `cli.py` | run/status/pause/resume/cancel/doctorの安定した終了条件 |

domain objectは原則immutableに保ち、更新は新しい値として生成する。外部入力、永続state、
provider event、CLI引数は境界で型、範囲、時刻、pathを検証する。

### 4.3 Refactor

Green後に責務、重複、命名を整理し、同じテストを再実行する。次の挙動を暗黙に変更
しない。

- task stateと合法遷移
- queue順序とretry/front requeue
- capacityのscope、confidence、freshness、reset時刻
- persisted schemaとrestart semantics
- CLIのexit codeとuser-visible output
- retry、failure、capacity event、actual Agent switchの個別counter

## 5. Phase別の安全ゲート

### Phase 0: Assumption Validation

Claude Code、Codex CLI、GitHub CLIの挙動は推測で実装しない。live probeは明示的に
opt-inし、subscription/billingを確認した最小promptだけを使う。結果はsecret、prompt、
個人pathを除去したfixtureとして保存する。UNKNOWNを成功扱いしない。

Bernsteinをworktree backendとして実装する前に、SPEC §58〜59のcompatibility spikeと
Adopt / Reject / Defer判断を完了する。

### Phase 1: Queue MVP hardening

Fake workerでqueue、state、capacity、restartを完成させる。repository lock、real Git
worktree、Agent contract、task-start handoff、安全なevent loopが揃うまでnative workerを
有効化しない。

### Phase 2以降: Native execution

native実行を開放する変更は、少なくとも次を満たす。

- subprocessはargv配列、固定cwd、environment allowlist、timeout、output上限を使う
- process group全体をSIGTERM → grace period → SIGKILLで終了できる
- 実行ファイルのpath/version、sandbox、approval mode、startup時のauth/billing modeを検証し、
  未確認または想定外なら起動しない
- Issue本文をdelimiter/schemaで囲んだuntrusted dataとしてpromptへ渡し、Issue由来値をcwd、
  argv、command、environmentへ昇格させない
- stdout/stderrだけでなくprompt、argv、audit logからもsecretとcredentialを除去する
- 対象repositoryのAGENTS.mdとCLAUDE.mdにAgent contractがある
- task fileとsemantic handoffをdispatch前にreadback検証する
- dirty worktreeを破壊せず別Agentが継続できる
- API fallback、metered usage、merge、release、deployは無効のままにする

push/PR機能はnative worker権限と分離する。PR本文は`Implements work for #N`を使い、
`Fixes #N`や`Closes #N`でIssueを自動closeしない。

## 6. セキュリティと復旧

変更ごとに次を確認する。

- token、credential、Issue本文、raw Agent outputをログやfixtureへ保存しない
- subprocessへ渡すenvironmentをallowlist化し、親processのsecretを継承させない
- GitHubのread-only discovery credentialとpush/PR write credentialを別tierにし、write tokenを
  worker environmentへ渡さない。write tokenからmerge、Actions、release、deploy権限を除く
- repository、branch、Issue番号、worktree pathを境界で検証する
- repository root外へのpath escapeとsymlinkをdispatch・mkdir前に拒否する
- security-sensitive Issueはtask/worktree/prompt生成前にeligibilityから除外する
- state directory/fileを原則0700/0600とし、temp file、fsync、atomic replace、readbackを維持する
- path-based checkだけに依存せず、dirfd、no-follow、親component検査でsymlink swapとTOCTOUを
  抑え、revision/CASでlost updateを検出する
- corrupt stateとfuture schemaを空stateとして上書きしない
- 破損stateをquarantineし、復旧用backup、retention、disk usage上限を設ける
- scheduler lock、PID、process start time、nonceで二重実行とPID reuseを防ぐ
- handoffを再構築できない場合は推測で再開せずNEEDS_HUMANにする
- worktree、handoff、checkpointはcancelやfailureでも削除しない

依存関係を変更した場合は次も実行する。

```bash
uv lock --check
uv export --frozen --no-hashes --no-emit-project | uvx pip-audit -r /dev/stdin
```

上記audit commandは現CIとの一致を優先した暫定形である。`pip-audit`をdev dependencyとして
`uv.lock`へ固定した後は、local/CIとも`uv run pip-audit`へ切り替える。未固定のaudit toolを
実行する際も不要なcredentialをenvironmentへ渡さない。

脆弱性を無視してgateを通さない。修正版がない場合は影響、到達可能性、暫定対策をIssue
またはPRへ記録する。

## 7. 品質ゲート

PR前にCIと同じコマンドを実行する。

```bash
uv sync --frozen --extra dev
uv run ruff check .
uv run mypy src
uv run pytest --cov=subsched --cov-report=term-missing --cov-fail-under=80
uv export --frozen --no-hashes --no-emit-project | uvx pip-audit -r /dev/stdin
```

総合coverageは80%以上を維持する。branch-only coverageのCI gateが導入されるまでは、
CIがbranch-only no-regressionを自動判定していると表現しない。必要なPRではpytest後に
coverage JSONの`covered_branches / num_branches`を確認し、PRへ値を記録する。

```bash
uv run coverage json -o /tmp/subsched-coverage.json
uv run python -c 'import json; d=json.load(open("/tmp/subsched-coverage.json"))["totals"]; print(d["covered_branches"] / d["num_branches"] * 100)'
```

H4完了前はbase branchのPR/CI記録と手動比較し、初期baseline 72.30%以上かつbase比で低下
させない。H4で同じ計算をlocal/CIへ実装した後は、base比で低下させず80%以上を自動強制
する。branchが0本の場合の定義もH4で固定する。

全suiteの前に対象testだけを実行してよいが、PR前には全gateを実行する。live provider、
GitHub write、長時間soak testは通常CIへ混ぜず、明示承認された隔離環境で実行結果を
記録する。実行できないgateをPASSと表現しない。

## 8. ドキュメントとstate schemaの同期

次の変更は、コードと同じPRで文書を更新する。

| 変更 | 同期先 |
| --- | --- |
| CLI、default、利用可能なPhase | README、SPEC、`--help` test |
| task/capacity state、遷移、不変条件 | SPEC、model/transition test |
| persisted schema、directory layout、migration | SPEC、recovery test、runbook |
| Agent contract、handoff、checkpoint | SPEC、AGENTS.md、CLAUDE.md、fixture |
| 設定schema | SPEC、`examples/scheduler.yaml`、config test |
| 開発・検証手順 | このWORKFLOW |

SPECから逸脱する実装を先にmergeしない。別のstate layoutや延期されたUXを採用する場合、
同等の安全性を示し、SPEC変更を先に承認する。

## 9. コミットとPull Request

対象ファイルだけをstageする。

```bash
git status --short --branch
git diff --check
git diff
git add <explicit-files>
git diff --cached --check
git commit -m "<type>: <description>"
git status --short --branch
(
  current_branch="$(git branch --show-current)" || exit 1
  default_branch="$(gh repo view takurot/agent-scheduler --json defaultBranchRef --jq .defaultBranchRef.name)" || exit 1
  repository="$(gh repo view takurot/agent-scheduler --json nameWithOwner --jq .nameWithOwner)" || exit 1
  origin_fetch="$(git remote get-url --all origin)" || exit 1
  origin_push="$(git remote get-url --push --all origin)" || exit 1
  printf '%s\n' "$current_branch" | grep -Eq '^issue/[1-9][0-9]*-[a-z0-9]+(-[a-z0-9]+)*$' || exit 1
  test -n "$default_branch" && test "$current_branch" != "$default_branch" || exit 1
  test "$repository" = "takurot/agent-scheduler" || exit 1
  test "$(printf '%s\n' "$origin_fetch" | wc -l | tr -d ' ')" -eq 1 || exit 1
  test "$(printf '%s\n' "$origin_push" | wc -l | tr -d ' ')" -eq 1 || exit 1
  test "$origin_fetch" = "$origin_push" || exit 1
  test "$origin_push" = "ssh://git@github.com:22/takurot/agent-scheduler.git" || exit 1
  test -z "${GIT_SSH-}${GIT_SSH_COMMAND-}" || exit 1
  test -z "$(git config --get core.sshCommand || true)" || exit 1
  ssh_config="$(ssh -G github.com 2>/dev/null)" || exit 1
  test "$(printf '%s\n' "$ssh_config" | awk '$1 == "hostname" {print $2}')" = "github.com" || exit 1
  test "$(printf '%s\n' "$ssh_config" | awk '$1 == "user" {print $2}')" = "git" || exit 1
  test "$(printf '%s\n' "$ssh_config" | awk '$1 == "port" {print $2}')" = "22" || exit 1
  ! printf '%s\n' "$ssh_config" | grep -Eq '^(proxycommand|proxyjump) ' || exit 1
  git push -u origin "HEAD:refs/heads/$current_branch"
)
```

Conventional Commitのtypeは`feat`、`fix`、`refactor`、`test`、`docs`、`ci`、`chore`、
`perf`から選ぶ。無関係なworktree変更、`.ai/` runtime state、secret、cacheを含めない。
push前にcurrent branchが意図した`issue/...`であり、main/default branchでないこと、remote、
HEAD、upstreamをcredentialを表示しないread-only commandで確認する。`git push`はユーザー
またはtaskから明示的に許可された場合だけ実行し、force pushは使用しない。mainへの反映は
PR、review、green CIを経由し、可能ならGitHub branch protectionでCIをrequired checkにする。

初期repository文書やCIを導入するbootstrapに限り、ユーザーが対象ファイルとmainへの直接
pushを明示した場合は例外を認める。通常のIssue実装、code、state schema、dependency変更
にはこの例外を使わない。全品質gate、secret scan、correctness/security reviewの完了後、
次の順で同期と対象を再確認する。検証とpushを同じsubshellへ閉じ込め、失敗後にpushへ進ま
ないようにする。

```bash
(
  origin_fetch="$(git remote get-url --all origin)" || exit 1
  origin_push="$(git remote get-url --push --all origin)" || exit 1
  test "$(printf '%s\n' "$origin_fetch" | wc -l | tr -d ' ')" -eq 1 || exit 1
  test "$(printf '%s\n' "$origin_push" | wc -l | tr -d ' ')" -eq 1 || exit 1
  test "$origin_fetch" = "$origin_push" || exit 1
  test "$origin_push" = "ssh://git@github.com:22/takurot/agent-scheduler.git" || exit 1
  test -z "${GIT_SSH-}${GIT_SSH_COMMAND-}" || exit 1
  test -z "$(git config --get core.sshCommand || true)" || exit 1
  ssh_config="$(ssh -G github.com 2>/dev/null)" || exit 1
  test "$(printf '%s\n' "$ssh_config" | awk '$1 == "hostname" {print $2}')" = "github.com" || exit 1
  test "$(printf '%s\n' "$ssh_config" | awk '$1 == "user" {print $2}')" = "git" || exit 1
  test "$(printf '%s\n' "$ssh_config" | awk '$1 == "port" {print $2}')" = "22" || exit 1
  ! printf '%s\n' "$ssh_config" | grep -Eq '^(proxycommand|proxyjump) ' || exit 1
  test "$(gh repo view takurot/agent-scheduler --json nameWithOwner --jq .nameWithOwner)" = "takurot/agent-scheduler" || exit 1
  git fetch origin main || exit 1
  set -- $(git rev-list --left-right --count main...origin/main)
  test "$1" -eq 0 && test "$2" -eq 0 || exit 1
  git add -- AGENTS.md CLAUDE.md docs/WORKFLOW.md || exit 1
  git diff --cached --check || exit 1
  test "$(git diff --cached --name-only)" = "$(printf '%s\n' AGENTS.md CLAUDE.md docs/WORKFLOW.md)" || exit 1
  git commit -m "docs: add project agent workflow" || exit 1
  test "$(git diff-tree --no-commit-id --name-only -r HEAD)" = "$(printf '%s\n' AGENTS.md CLAUDE.md docs/WORKFLOW.md)" || exit 1
  git show --format= HEAD -- AGENTS.md CLAUDE.md docs/WORKFLOW.md | \
    grep -Eiq '(gh[pousr]_[A-Za-z0-9_]{10,}|github_pat_[A-Za-z0-9_]+|AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9_-]{20,}|AIza[A-Za-z0-9_-]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|BEGIN [A-Z ]*PRIVATE KEY)' && exit 1
  set -- $(git rev-list --left-right --count main...origin/main)
  test "$1" -eq 1 && test "$2" -eq 0 || exit 1
  git push origin main:main
)
```

この例外でもforce pushは禁止する。対象ファイルや期待commit数が異なるbootstrapでは、
コマンドを流用せず、明示されたscopeに合わせて検査値を更新する。

PR本文には次を記載する。

- 対応IssueとSPEC節
- 変更内容と理由
- state、capacity、billing、security、recoveryへの影響
- 実行したunit/integration/E2Eと品質ゲート
- 実行できなかったlive検証と理由
- migration、rollback、NEEDS_HUMAN条件

このrepository自身の開発PRは、受け入れ条件を満たしてmerge時にIssueを閉じる場合、
`Closes #<ISSUE>`を使用できる。一方、完成したSchedulerが対象repositoryへ自動生成する
PRでは`Implements work for #<ISSUE>`を使い、`Fixes` / `Closes`による自動closeを禁止する。

## 10. レビューと完了条件

コード変更はmerge前にcorrectnessとsecurityの両面でレビューする。指摘は重大度だけで
採否を決めず、再現手順、source-to-sink、SPEC、テストで検証する。High以上の妥当な
問題はmerge前に修正し、回帰テストを追加する。

Issueは次をすべて満たした場合だけ完了とする。

- [ ] Issueのチェックリストと受け入れ条件を満たした
- [ ] 依存IssueとPhase gateを満たした
- [ ] Red → Green → Refactorの証跡がある
- [ ] unit、integration、E2Eの該当層を更新した
- [ ] lint、typecheck、test、coverage、dependency auditが通った
- [ ] billing/auth/capacity/schemaのunknown pathがfail-closedである
- [ ] restart、retry、cancelでstate/worktree/handoffを破壊しない
- [ ] README、SPEC、設定例、runbookが実装と一致する
- [ ] PRに検証結果と未検証条件を記録した

ファイル構成は変化するため、この文書に静的tree snapshotは置かない。確認時は
`git ls-files`とGitHub Issueを正とする。
