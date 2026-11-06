These are synthetic consumer fixtures, not captured live CLI output.

Issue #552 corrects the old `pending-exit8.json` and `fail-exit1.json` names to
`pending-exit0.json` and `fail-exit0.json`; the JSON bodies are unchanged.
[gh v2.100.0 checksRun](https://github.com/cli/cli/blob/v2.100.0/pkg/cmd/pr/checks/checks.go#L177-L179)
returns the JSON exporter result before its ordinary failed/pending exit handling
(lines 233–239). Successful JSON export can therefore exit 0 for PASS, PENDING,
or FAIL. The first version with this behavior has not been established.

The issue investigation reports a live gh 2.100.0 comparison: PR #547's five
failed checks exited 0 with JSON and 1 without; PR #551's successful checks exited
0 in both modes. That observation was supplied with the issue; it is not a live
pending observation or a capture made during this implementation. The worker
does not have gh installed, so it cannot repeat that comparison. Regression tests
mock subprocess results and separately retain conservative exit 1/8 compatibility.
