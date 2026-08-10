from pathlib import Path

# Default on-disk backup-store root, under the operator's working directory.
DEFAULT_STORE_DIR: Path = (Path(".uwazi_admin_agent") / "runs").resolve()

# Safety cap: refuse to execute a run whose touch set exceeds this many entities.
MAX_ENTITIES_PER_RUN: int = 1000

# Number of entities to apply per batch during execute. Consumed in Phase 5.
EXECUTE_BATCH_SIZE: int = 50
