"""Runs on calibre's device thread, serialized with the Kobo driver's own jobs."""

import os


def run_sync(device, tasks, backup_dir, keep_backups, write=True):
    from calibre_plugins.kobo_highlight_keeper.core import kobodb
    from calibre_plugins.kobo_highlight_keeper.core.engine import sync_books

    mount = device._main_prefix
    db_path = os.path.join(mount, '.kobo', 'KoboReader.sqlite')
    backups = []

    def backup():
        backups.append(str(kobodb.backup(db_path, backup_dir, keep_backups)))

    transaction = getattr(device, 'database_transaction', None)
    if transaction is not None:
        # The driver's connection: holds calibre's kobo_db_lock and copies the
        # database back if the driver had to work on a local copy.
        with transaction(use_row_factory=True) as conn:
            results = sync_books(conn, mount, tasks, backup, write=write)
    else:  # calibre < 8
        conn = kobodb.connect(db_path)
        try:
            results = sync_books(conn, mount, tasks, backup, write=write)
            conn.commit()
        finally:
            conn.close()
    return {'results': results, 'backups': backups}
