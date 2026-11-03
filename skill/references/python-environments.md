# Agent-managed Python environments

This protocol is for Python workloads on the selected node. The agent
manages environments with reviewed, one-off commands; it does not add a
remote user-provisioning script. A normal run uses the recorded inventory hint
and a task-declared short environment check. It does not perform a full
environment scan or reinstall packages when the check passes. If the check (or
a direct workload) reports an environment mismatch, the runner stops and
returns the failure with the reviewed repair candidate. The agent then
diagnoses the node, runs a reviewed repair command through
`adaptive-runner repair`, rechecks it, and reports the result to the resource
service. The service revalidates the record with a read-only inventory probe.

## Storage contract

For an assignment, `work_dir` is the intermediate run directory on the
selected node. Unless the manifest explicitly sets `work_dir`, use
`<storage_path>/data_<ssh_user>/adaptive`.

All data, model, and environment files stay below `work_dir`:

```text
work_dir/
├── data/           # declared data artifacts
├── models/         # declared model artifacts
├── projects/<project>/ # declared source artifacts
└── envir/
    ├── cache/uv/       # UV_CACHE_DIR
    ├── envs/<project>/ # UV_PROJECT_ENVIRONMENT or uv venv roots
    ├── python/         # UV_PYTHON_INSTALL_DIR
    ├── tmp/            # TMPDIR
    └── tools/uv        # downloaded uv binary
```

Before creating anything, the agent verifies that `storage_path` is a mounted
filesystem and is writable by the execution user. It then runs a quoted,
reviewed `mkdir -p` for the directories above. Existing directories are
reused. It must never silently fall back to `/`, `~/.cache`, `~/.local`, or a
system Python directory. A failed write check stops the task and asks an
administrator to provision permissions.

## Source, model, and data artifacts

Environment readiness includes the source code, model files, and data files
declared in a task's `artifacts` list. Each item names a `source`, a `kind` of
`source`, `model`, or `data`, and an optional destination template. A local
source is a file or directory path resolved from the directory where
`adaptive-runner plan` is executed, and the planner records its size. Source
artifacts default to `{work_dir}/projects/<name>` and must use a local
controller path. Network URLs and remote source discovery are rejected.

An open model or dataset may use `modelscope:<owner>/<name>`. Model acquisition
uses this fixed order for every assignment:

1. Check the destination on the target node. A directory must contain entries
   and every path in `required_files`; a file may additionally require
   `sha256`.
2. If the model is unavailable and a ModelScope id is declared, run
   `modelscope download` directly on that target node. Remove `HTTP_PROXY`,
   `HTTPS_PROXY`, `ALL_PROXY`, and their lowercase forms from the process.
3. If ModelScope fails or its result does not pass validation, transfer the
   declared controller-local `source` with `rsync --checksum`. Remote rsync
   uses the reviewed SSH connection. This is the final fallback because model
   payloads should avoid SSH whenever direct ModelScope acquisition succeeds.

A model with both acquisition paths declares a local `source` and a
`modelscope` id. A ModelScope-only model uses `modelscope:<owner>/<name>` as
`source`; failure stops the task because no controller-local fallback was
declared. The target node needs the `modelscope` command line tool when the
model is missing. ModelScope always runs without a proxy and never uses the
package-download proxy tunnel.

A ModelScope dataset retains controller-side caching. The cache lives next to
the plan as `.adaptive-computation/modelscope` and can be moved with
`run --artifact-cache <dir>`. The controller downloads one snapshot without a
proxy and synchronizes it to every assignment.

Before the environment check and preflight, the controller uploads declared
source and data artifacts with `rsync --checksum`; model artifacts follow the
ordered acquisition protocol above. The current machine uses local rsync
without opening SSH. Directory transfers compare each file through rsync
checksums; file artifacts can also provide `sha256`, which is verified at the
source and destination. Transfers remain resumable and never delete unrelated
destination files. A failed final transfer stops the task.

Completed artifact verifications are cached as markers under
`<artifact-cache>/verified/` (the directory selected with
`run --artifact-cache`). A marker stores the node, the resolved destination,
a stat fingerprint of the declared source (or the resolved ModelScope
snapshot), and an inventory digest of the verified destination. A later run
with a matching marker performs one metadata-only revalidation and skips the
sha256 digest comparison and the `rsync --checksum` transfer, reporting
`verify_cache: "hit"` in the result. A changed source tree, changed destination
metadata, or a damaged marker falls back to the full verification path and
rewrites the marker. Delete the `verified/` directory to force a complete
re-verification.

## Lightweight check and record update

The plan may contain `environment_check`, a short shell command expanded with
`{work_dir}`, `{disk_path}`, `{node_alias}`, `{gpu_index}`, `{rank}`, and
`{world_size}`. The dispatcher runs it in direct mode immediately before the
ordinary preflight/workload. It should verify only the recorded runtime and
locked imports, for example `uv --version`, the project lockfile, the selected
Python executable, and one small import. It must not run `uv sync`, scan a
home directory, or download anything.

Before dispatch, use `adaptive-runner inspect --uv-cache --node <alias>` to
list cached packages, Python versions, and project lock files. Read a lock with
`uv export --project <project> --format requirements-txt --no-hashes`; do not
parse the lock format by hand. When every package in the lock is present in the
cache, `uv sync --frozen --offline` can build or verify the environment without
network access. Missing packages require a reviewed online sync.

The task may name an `environment_repair_task` or provide an explicit
`environment_repair_command`. These fields describe a reviewed repair
candidate; the runner returns them with an environment failure and does not
execute them automatically. Mark reusable repair tasks with
`environment_maintenance: true`; a normal full-plan run skips them. After the
agent repairs an affected node, it reports the structured environment record
through `adaptive-runner repair --report`. The resource service stores the
record, publishes a new snapshot version, and revalidates it with a read-only
probe. The report contains paths, versions, and a check exit code only;
command output stays in the local repair record under
`.adaptive-computation/repairs/`.

The local audit record may be stored under `.adaptive-computation/`, but it
contains paths, versions, lockfile hashes, and command status only. It must not
contain private keys, proxy credentials, or full secret-bearing environment
values. The lightweight fields in the snapshot remain hints; the short check
is authoritative for the current run, and a reported environment record is
revalidated by the service before the planner treats it as ready.

Conda, system Python, and arbitrary global `pip install` are not environment
maintenance mechanisms for this skill. Existing Conda environments can be
reported as legacy resources, but a new or repaired project environment uses
uv. A task that explicitly requests an unrecorded conda, micromamba, or venv
environment fails planning; recorded legacy environments are read-only.

## Install or repair with uv

Environment setup is a remote write and requires approval after the plan is
reviewed. It is normally invoked lazily by the run path, not as a prerequisite
for every run. The agent:

1. obtains or reuses a pinned uv version and places the binary under
   `<work_dir>/envir/tools/uv` when no suitable uv is available;
2. sets, for every setup command, `UV_CACHE_DIR`,
   `UV_PYTHON_INSTALL_DIR`, `UV_TOOL_DIR`, `TMPDIR`, and, for a project,
   `UV_PROJECT_ENVIRONMENT` to paths below `<work_dir>/envir`;
3. uses the project's lockfile, normally `uv sync --frozen`, and does not
   mutate a lockfile without explicit approval;
4. uses `uv python install <version>` and `uv venv <path>` when a runtime or
   environment must be created;
5. records the uv version, Python version, environment path, lockfile hash,
   and setup result, then reports the environment through
   `adaptive-runner repair --report` before the next reviewed run.

Downloads use validated HTTPS and a temporary file on the data disk. Do not
pipe an unreviewed network response directly to a shell. Do not install uv or
Python into `/usr`, `/opt`, `/root`, or the user's default cache directories.

## Proxy through the current machine

Only use a proxy tunnel when direct HTTPS fails or the user has requested it,
and only for package-index and tool downloads. ModelScope model and dataset
downloads must stay direct and must not use this tunnel.
The agent must know the local proxy protocol and port; it must not guess a port
or print a proxy URL containing credentials. A `ProxyJump` changes the route
for SSH itself and does not make remote HTTP downloads use the local proxy.
For remote downloads, use a loopback-only SSH reverse forward in a separate,
reviewable alias. Take `HostName`, `Port`, `User`, `IdentityFile`, and
`RemoteForward` from the `ssh_proxy` block of the gitignored `local.json`; the
shape is:

```sshconfig
Host <proxy-alias>
    HostName <controller-host>
    Port <ssh-port>
    User <ssh-user>
    IdentityFile ~/.ssh/<key-file>
    IdentitiesOnly yes
    BatchMode yes
    StrictHostKeyChecking accept-new
    ExitOnForwardFailure yes
    RemoteForward 127.0.0.1:<REMOTE_FORWARD_PORT> 127.0.0.1:<LOCAL_PROXY_PORT>
```

The agent writes only this connection metadata and the identity-file path;
never a key or proxy credential. It starts a dedicated tunnel only after
approval:

```bash
tunnel_pid=''
cleanup_tunnel() {
    rc=$?
    trap - EXIT INT TERM HUP
    if [ -n "$tunnel_pid" ] && kill -0 "$tunnel_pid" 2>/dev/null; then
        kill -TERM "$tunnel_pid" 2>/dev/null || true
        for _ in 1 2 3 4 5; do
            kill -0 "$tunnel_pid" 2>/dev/null || break
            sleep 1
        done
        kill -KILL "$tunnel_pid" 2>/dev/null || true
    fi
    if [ -n "$tunnel_pid" ]; then
        wait "$tunnel_pid" 2>/dev/null || true
    fi
    exit "$rc"
}
trap cleanup_tunnel EXIT INT TERM HUP

ssh -F ~/.ssh/config -N -T \
  -o ExitOnForwardFailure=yes <proxy-alias> &
tunnel_pid=$!
# Wait for the forward to be ready and abort if this exact child exits.
# Run the approved download in this same shell; cleanup_tunnel always runs.
```

The remote download command then uses the matching loopback endpoint, for
example `HTTPS_PROXY=http://127.0.0.1:<REMOTE_FORWARD_PORT>` (or the appropriate
`socks5h` form).

The agent must manage the tunnel as a bounded child process, not as an
untracked background service. It records the PID and installs cleanup for
`EXIT`, `INT`, `TERM`, and timeout/cancellation paths. Cleanup must terminate
that exact PID, wait for it, and use a final bounded `KILL` only if necessary.
Do not use `ssh -f` unless its control process can be tracked and explicitly
closed. Use a run-specific remote forward port with
`ExitOnForwardFailure=yes`; never kill a broad `ssh`/`sshd` pattern. After the
proxy download/setup finishes, verify that the child has exited and that the
remote loopback port is no longer listening before starting another download.
If a temporary SSH config include or alias was created, remove it only after
the tunnel has been reaped. The tunnel binds on remote loopback only, is closed immediately after setup,
and is not enabled globally. If forwarding is refused or the proxy is
unreachable, stop and report the error; never weaken host verification or fall
back to a password.

## Updates and cleanup

To update an environment, the agent first shows the proposed lockfile or
package change, runs the approved repair command with the same data-disk paths,
and performs the short check again. It may clean uv caches only after checking
ownership and active jobs. It must not remove another user's environment,
truncate active logs, or delete project/model data as part of environment
maintenance.
