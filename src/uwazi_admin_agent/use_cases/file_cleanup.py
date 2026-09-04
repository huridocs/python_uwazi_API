"""The dedupe nominator's flow context (per ONE entity).

The dedupe discovery + delete now run through the shared file-deletion core
(:mod:`...use_cases.file_deletion`):

- :func:`cleanup_plan_with_bytes` is the discovery half, and it runs for
  BOTH the real cleanup and its dry-run rehearsal: fetch the entity's raw →
  the connection-cited file ids (:func:`cited_file_ids`) → the uploaded-file
  refs (:func:`extract_file_refs`) → each ref's bytes → digest → the pure
  :func:`plan_entity_cleanup` decision. Unlike the historical flow it KEEPS
  the fetched bytes — the core backs them up before deleting, at zero extra
  GETs (dedupe is the nominator that already held every file's bytes for
  hashing).
- :func:`delete_files_with_backup` applies the plan: per ``to_delete`` ref,
  persist the bytes-in-hand backup BEFORE the delete call (the revert
  precondition), then delete. Soft failures never raise (a server-refused
  delete counts as ``failed``); a hard port EXCEPTION propagates so the
  caller's error policy kills the script BEFORE any further deletes.

Cache freshness (invalidate-then-refetch): a delete mutates the FILES
collection — not the entity row — so the recording seam
(:meth:`BackupIntercept._record_deleted_files`) drops the affected entity's
cached raw (all language rows; the JOIN view is shared) and evicts the
deleted files' cached bytes right after the batch joins, before any hard
error is re-raised. A re-run therefore rediscovers live truth: it cannot see
ghost refs of already-deleted files, and the restored duplicates (fresh ids
after a revert) are visible to the very next read. Entries repopulate lazily
on the next read — cache eviction is lossless by construction (the cache is
a read-through mirror; the truth is Uwazi + the run's backup store).

Manifest recording is the bound helpers' job (script thread, after the
deletion batch joins, successful deletes only — see
:mod:`...use_cases.parallel_script_helpers`).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

# Runs ONE coroutine to completion: asyncio.run or loop.run_until_complete.
CoroRunner = Callable[[Any], Any]
