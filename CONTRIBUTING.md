# Development and release

Run `python -m pytest -q`, `ruff check gateway deployment-center release-tools tests`, `python -m compileall -q gateway deployment-center release-tools`, `bash -n installer/install.sh`, and `python scripts/secret_scan.py` before committing.

Do not commit `.env`, credentials, databases, session files, uploads, logs, private keys, wheelhouses, or Release binaries.

A Release is built with `./.venv/bin/python release-tools/release.py release --version X.Y.Z`. Publish only after tests pass, verify the signed public manifest, create an annotated `vX.Y.Z` tag, and push the branch and tag without force.
