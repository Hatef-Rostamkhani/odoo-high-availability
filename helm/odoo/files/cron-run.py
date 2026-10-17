"""Exit a cron-only process when its scheduler thread dies; kubelet restarts it."""

import os
import runpy
import threading

default_hook = threading.excepthook


def scheduler_failed(args):
    if args.thread.name.startswith('odoo.service.cron.'):
        # Exceptions can include connection details. Emit only the exception type.
        print(
            'Cron scheduler terminated: '
            + args.exc_type.__name__
            + '; restarting the cron process',
            flush=True,
        )
        os._exit(1)
    default_hook(args)


threading.excepthook = scheduler_failed
runpy.run_path('/usr/bin/odoo', run_name='__main__')
