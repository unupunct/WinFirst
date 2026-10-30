# Live acceptance tests

These tests change real Windows settings. They exist to close the one gap the
unit tests cannot: *does the apply path actually work on a real machine?*

**Run them only on a disposable virtual machine** (Hyper-V, VMware, VirtualBox,
or a fresh Windows Sandbox). Never run them on a workstation you care about.
Every scenario restores the state it changed, but a crash or a bug can still
leave a service, task or setting behind.

## What the scenarios cover

| Scenario | What it does | What it restores |
| --- | --- | --- |
| `settings` | Toggles one reversible Explorer setting through the real settings registry, verifies the read-back, then restores it. | The original value. |
| `power` | Activates a different power plan, reads the GUID back, then reactivates the original plan. | The original active plan. |
| `service` | Creates a throw-away service (`WinFirstAcceptanceSvc`), changes its startup mode through the service spec, verifies, then deletes it. | Deletes the test service. |
| `task` | Creates a scheduled task under `\WinFirstAcceptance`, disables and re-enables it through the task spec, then deletes it. | Deletes the test task. |
| `cleanup` | Creates a sandbox folder under `%TEMP%`, deletes it through the cleanup spec, and checks the folder is empty. | Deletes the sandbox folder. |
| `restore` | Creates a real System Restore checkpoint (slow, and Windows may reuse a recent one). | The checkpoint stays; remove it manually if desired. |

Each scenario runs the full controller lifecycle (validate -> apply -> verify ->
audit), so the evidence also proves history recording and verification.

## Running

From an **elevated** PowerShell prompt on the test VM:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\acceptance\run_acceptance.ps1 -ConfirmDisposableVM
```

Useful options:

```powershell
# One scenario only
... -Scenario settings

# Take a Hyper-V checkpoint first (recommended)
... -ConfirmDisposableVM -HyperVVm "Win11-Lab"

# Include the slow restore-point scenario
... -Scenario restore
```

The script refuses to run without `-ConfirmDisposableVM`. It writes:

- `acceptance-results\<timestamp>\results.xml` - JUnit results
- `acceptance-results\<timestamp>\evidence.json` - observations recorded by the tests
- `acceptance-results\<timestamp>\transcript.log` - full console output
- `acceptance-results\<timestamp>\summary.json` - machine info, pass/fail counts, exit code

Exit codes: `0` all scenarios passed, `1` at least one failed, `2` prerequisites
missing (Python, pytest), `3` confirmation flag missing.

## Prerequisites

- Windows 10 1809+ or Windows 11, x64.
- Python 3.11+ with the project requirements installed.
- Administrator rights (the service, task and restore scenarios need them).
- `service` and `task` scenarios also require the Windows PowerShell modules
  `ScheduledTasks` (present on all supported builds).

## Recording the result

The repository's README distinguishes verified from unverified behaviour. After
a successful run, update the Verification status section with the date, the
Windows build, and the scenarios that passed, and keep the `summary.json` with
the release evidence. Do not claim verification for scenarios that were skipped.
