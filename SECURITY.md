# Security policy

## Reporting a vulnerability

Please report security issues privately through GitHub's **Security →
Report a vulnerability** (private vulnerability reporting) on this
repository rather than in a public issue. Include steps to reproduce and the
affected version (`rsostb --version`). We aim to acknowledge reports within
a few days.

In scope, in particular:

* **Sandbox escapes** — model-written code reaching the network, the host
  filesystem, other processes, or environment secrets
  (see [docs/SANDBOX.md](docs/SANDBOX.md));
* **Submission integrity** — a results file that is accepted with scores it
  did not earn, or that bypasses version/hash checks;
* **Leaderboard / Space** — injection through uploaded metadata, resource
  exhaustion through crafted uploads, exposure of stored responses or tokens;
* **Data issues** — anything that looks like real personal data, private
  (hidden) tasks in the public tree, or harmful operational content in a task.

## Hardening notes for operators

* Evaluate untrusted models with `--sandbox docker`. The `process` backend is
  layered best-effort isolation, not a container.
* Keep `HF_TOKEN` and model API keys in environment variables or CI/Space
  secrets; `rsostb` never writes them to disk.
* The Space never executes submitted code; execution-verified status is
  produced offline by maintainers.

## Supported versions

Security fixes are made on the latest release line.
