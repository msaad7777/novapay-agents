import os
import tempfile
from pathlib import Path

from novapay_agents.generate import generate


class Estate:
    """A freshly generated estate and an empty state folder, set through env vars."""

    def __enter__(self):
        self.tmp = Path(tempfile.mkdtemp())
        generate(self.tmp / "novapay")
        self.old = {k: os.environ.get(k) for k in ("NOVAPAY_DATA", "NOVAPAY_STATE", "NOVAPAY_MAX_PENDING")}
        os.environ["NOVAPAY_DATA"] = str(self.tmp / "novapay")
        os.environ["NOVAPAY_STATE"] = str(self.tmp / "state")
        os.environ.pop("NOVAPAY_MAX_PENDING", None)
        return self

    def __exit__(self, *exc):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
