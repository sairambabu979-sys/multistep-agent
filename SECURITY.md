# Security

## Reporting a vulnerability

Please report it privately, through GitHub's **Report a vulnerability** button on the
repository's Security tab (a private security advisory), not in a public issue. Say what
you found, how to reproduce it, and what it lets someone do.

## The trust model, in one paragraph

msa runs model-written code and the repository's own code (its tests, its conftest, a
mypy plugin). The **local** sandbox backend confines paths, scrubs the environment and
bounds every command, but repository code runs as you: use it only for repositories you
trust. For any other, set `[sandbox].backend = "container"`
([ADR-019](docs/adr/019-container-sandbox.md)). Agents never hold more authority than the
harness grants their role. Every gate is decided by code, and nothing is released without
a person's approval.

## The checklist (PLAN M10)

Each control names the test that proves it. A unit test checks that every test named here
exists (`tests/unit/test_security_checklist.py`), so a control cannot point at nothing.
**Open** items are gaps no test closes. Each one says what would close it, or why it is
accepted.

### Commands

| Control | Evidence |
|---|---|
| Only allowlisted commands run, with typed arguments: no shell, no command line from the model. Path arguments are confined like any tool path. | `tests/unit/tools/test_sandbox.py::test_only_allowlisted_commands_run`, `tests/unit/tools/test_sandbox.py::test_pytest_runs_through_the_launcher_with_confined_paths` |
| Every command is bounded. A timeout kills its whole process group. Output keeps its head and tail. CPU time and file size are capped by `RLIMIT_*`. | `tests/unit/tools/test_sandbox.py::test_a_timeout_kills_the_whole_process_group`, `tests/unit/tools/test_sandbox.py::test_output_keeps_head_and_tail_and_counts_what_it_drops`, `tests/unit/tools/test_sandbox.py::test_the_cpu_limit_kills_a_spinning_command`, `tests/unit/tools/test_sandbox.py::test_the_file_size_limit_applies` |
| The environment is scrubbed to a passthrough list, and no setting can pass a credential-like variable through. | `tests/unit/tools/test_sandbox.py::test_the_environment_is_scrubbed_to_the_passthrough_list`, `tests/unit/tools/test_capabilities.py::test_credential_like_variables_can_never_be_passed_through` |
| Repository git config cannot run programs: no diff drivers, textconv, fsmonitor or hooks. A revision can never parse as an option. | `tests/unit/tools/test_sandbox.py::test_repository_config_cannot_run_programs_during_a_diff`, `tests/unit/tools/test_sandbox.py::test_git_diff_rejects_revisions_that_could_be_options` |

### The filesystem

| Control | Evidence |
|---|---|
| Paths are resolved once and confined, then opened by descriptor without following symlinks. Fuzzed against a reference model. | `tests/property/test_path_confinement.py::test_resolution_agrees_with_the_reference_model`, `tests/property/test_path_confinement.py::test_a_read_never_returns_anything_from_outside`, `tests/property/test_path_confinement.py::test_a_write_never_reaches_outside_the_tree` |
| A symlink swapped in mid-call is never crossed. | `tests/chaos/test_symlink_race.py::test_tools_never_cross_a_symlink_swapped_in_mid_call`, `tests/unit/tools/test_paths.py::test_a_symlink_that_appears_after_resolving_is_refused`, `tests/unit/tools/test_container_sandbox.py::test_a_regex_check_never_reads_a_symlink_out_of_the_worktree` |
| Writes are atomic and break hard links instead of writing through them. | `tests/unit/tools/test_workspace.py::test_an_edit_breaks_a_hard_link_instead_of_writing_through_it` |
| Each role writes only its own paths, checked on the resolved path. Control paths (settings, CI, the git pointer) are protected for every role. The Coder never changes the Tester's files. | `tests/unit/tools/test_workspace.py::test_capability_is_checked_on_the_resolved_path`, `tests/unit/tools/test_capabilities.py::test_control_paths_are_protected_for_every_role`, `tests/unit/tools/test_workspace.py::test_a_linked_worktrees_git_pointer_cannot_be_edited`, `tests/property/test_path_confinement.py::test_the_coder_never_changes_a_tester_file` |

### The network and untrusted repositories

| Control | Evidence |
|---|---|
| Agent commands and the integration suite run with no network (`unshare -rn`) where the kernel allows. With `isolate_network = "always"`, a machine that cannot is refused. CI requires it. | `tests/unit/tools/test_sandbox.py::test_an_agent_command_has_no_network_at_all`, `tests/unit/runtime/test_stages.py::test_an_isolated_suite_cannot_reach_the_network`, `tests/unit/runtime/test_stages.py::test_isolation_required_but_unavailable_blocks` |
| The container backend: one fresh container per command. Only the worktree is writable. No network, a non-root user, no capabilities, CPU, memory and process limits. End to end, the definition-of-done run passes every gate with each repository command in a container (CI). | `tests/integration/test_demo.py::test_the_definition_of_done_run`, `tests/unit/tools/test_container_sandbox.py::test_a_command_runs_locked_down`, `tests/unit/tools/test_container_sandbox.py::test_a_container_can_write_the_worktree_and_nothing_else`, `tests/unit/tools/test_container_sandbox.py::test_a_container_has_no_network`, `tests/unit/tools/test_container_sandbox.py::test_a_container_past_its_memory_limit_is_killed` |
| The container backend fails closed. With no usable runtime, a run refuses to start, and nothing falls back to running locally. | `tests/unit/tools/test_container_sandbox.py::test_no_runtime_installed_is_a_clear_refusal`, `tests/unit/tools/test_container_sandbox.py::test_a_live_run_refuses_to_start_without_a_runtime` |

### Secrets

| Control | Evidence |
|---|---|
| A task holding a secret is refused before it is journaled or stored, without repeating it. That applies on the command line and through the API. | `tests/unit/test_cli.py::test_run_refuses_a_task_holding_a_secret_before_journaling_it`, `tests/unit/service/test_api.py::test_a_task_holding_a_secret_is_refused_and_not_stored` |
| Skills and preferences holding a secret are refused. | `tests/unit/skills/test_registry.py::test_a_skill_holding_a_secret_is_refused_without_repeating_it`, `tests/unit/test_cli_skills.py::test_prefs_add_refuses_a_secret` |
| A secret anywhere in the change fails the security review, and blocks the release again, without leaking it into the report. | `tests/unit/runtime/test_stages.py::test_a_secret_and_an_eval_fail_the_security_review_without_leaking`, `tests/unit/runtime/test_stages.py::test_a_secret_in_the_patch_is_never_released` |
| The model key is held only by the CI step that spends. | `tests/unit/test_eval_workflow.py::test_only_the_spending_step_holds_the_model_key`, `tests/unit/test_live_run_workflow.py::test_the_release_never_holds_the_model_key` |
| **Open:** what an agent reads (repository files, command output) goes to the model unscanned. A secret committed to the repository can reach the model provider. PLAN §8 used to say a scanner checked outgoing prompts; none does. **Would close it:** scanning each request's tool results with `msa.secrets` before it is sent, and refusing or redacting. | none |

### Prompt injection and authority

| Control | Evidence |
|---|---|
| Capabilities are enforced by the harness, never by the prompt: see the filesystem rows above. | (above) |
| No agent can approve, rule, unblock or pause, and no anonymous principal can either. The core refuses an `agent:` principal. | `tests/unit/core/test_machine.py::test_no_agent_and_no_one_anonymous_can_answer_a_human_gate`, `tests/unit/core/test_machine.py::test_no_agent_can_pause_a_run`, `tests/unit/test_cli_gates.py::test_no_agent_and_nobody_can_answer` |
| Nothing is published before a person approves. | `tests/unit/core/test_machine.py::test_nothing_is_published_before_a_person_approves` |
| Each role is told that repository text is information, never instructions. **A mitigation, not a boundary:** an injected instruction can still steer what an agent writes within its own paths. The gates and the person approving are what stop it. | `tests/unit/agents/test_roles.py::test_every_role_is_told_repository_text_is_never_instructions` |

### What a change adds

| Control | Evidence |
|---|---|
| Insecure calls in the added lines (`eval`, shell commands, unpickling, unsafe YAML, TLS verification off) are found in the syntax tree and block. | `tests/unit/runtime/test_stages.py::test_insecure_calls_are_found_in_the_syntax_tree_on_added_lines_only` |
| A known-vulnerable pinned Python dependency the change adds blocks. | `tests/unit/runtime/test_stages.py::test_a_vulnerable_dependency_the_change_adds_blocks` |
| A path no role may write never reaches a release, whatever produced it. | `tests/unit/runtime/test_stages.py::test_a_path_no_role_may_write_is_never_released` |
| **Accepted:** an audit that cannot run (no pip-audit, no network) is reported and does not block ([ADR-020](docs/adr/020-dependency-audit.md)). Blocking would send the change to an agent that cannot fix the environment. | `tests/unit/runtime/test_stages.py::test_an_audit_that_cannot_run_is_reported_without_blocking` |

### Outward actions (push, pull request)

| Control | Evidence |
|---|---|
| Off by default (`[publish].mode = "local"`). The mode is chosen by the person who approves, and is refused for anything but an approved release. | `tests/unit/core/test_machine.py::test_an_approval_that_names_no_mode_publishes_locally`, `tests/unit/test_cli_gates.py::test_publish_is_refused_at_a_plan_gate`, `tests/unit/service/test_api.py::test_a_publish_mode_for_a_plan_gate_is_refused` |
| A push is never forced, and runs none of the repository's hooks. | `tests/unit/runtime/test_publishing.py::test_a_push_never_moves_a_remote_branch_that_holds_something_else`, `tests/unit/runtime/test_publishing.py::test_a_push_runs_no_hook_of_the_repository` |
| The token travels only in a request header. It is in no repr, and is scrubbed with credentials in URLs from every recorded error. | `tests/unit/runtime/test_publishing.py::test_the_rest_api_opens_a_pull_request_with_the_token_in_the_header_only`, `tests/unit/runtime/test_publishing.py::test_a_refusal_from_the_api_is_reported_without_the_token`, `tests/unit/runtime/test_publishing.py::test_credentials_in_urls_and_known_secrets_are_scrubbed` |

### Spending

| Control | Evidence |
|---|---|
| Spending is reserved before each effect, from disjoint slices. A run never commits more than its budget, and an overspend is recorded truthfully. | `tests/unit/core/test_invariants.py::test_budget_safety`, `tests/property/test_runs.py::test_a_live_run_never_has_more_committed_than_its_budget`, `tests/unit/core/test_budget.py::test_overspend_is_recorded_truthfully_and_flagged` |

### The HTTP API

| Control | Evidence |
|---|---|
| It listens on loopback unless configured otherwise, with no CORS origin by default. | `tests/unit/test_config.py::test_the_api_listens_on_loopback_unless_told_otherwise` |
| Every endpoint but `/health` needs a bearer token. Tokens name a person and are stored only as hashes. Each role can do exactly what it should. | `tests/unit/service/test_api.py::test_health_needs_no_token_and_everything_else_does`, `tests/unit/service/test_api.py::test_tokens_are_for_people_and_stored_only_as_hashes`, `tests/unit/service/test_api.py::test_each_role_can_do_exactly_what_it_should` |
| Unknown runs, bad ids and files outside the run's allowlist are refused. | `tests/unit/service/test_api.py::test_unknown_runs_bad_ids_and_files_outside_the_allowlist_are_refused` |
| **Accepted:** there is no TLS in the service itself. Exposing it beyond loopback is meant to go behind a TLS-terminating proxy (`msa.toml`, `[service]`). | none |

### The supply chain

| Control | Evidence |
|---|---|
| Every locked Python dependency is audited in CI (`make audit`, the `audit` job), and so are the workbench's production npm dependencies (`npm audit`, the `web` job). | CI: `.github/workflows/ci.yml` |
| Every workflow runs with `contents: read`. | CI: the `permissions` block of each workflow |
| **Open:** GitHub Actions are pinned to major-version tags (`actions/checkout@v5`), not commit SHAs. A compromised tag would run in CI, though only with read access, and the spending workflows hold the model key. **Would close it:** pinning every `uses:` to a full commit SHA, with Dependabot keeping them current. | none |

## Beyond this code

- **Container escape** is the container runtime's boundary, not msa's. Keep Docker or
  Podman current; rootless Podman narrows what an escape reaches.
- **The model provider** sees what the agents read and write. Do not point msa at code you
  may not send to Anthropic.
