"""main.py - puts the UI together and serves it (server.py and demo.py call run)."""

import socket
import sys

from labui.app import App, Server, make_handler
from labui.importer import Importer
from labui.plan import Catalogue
from labui.poller import Poller
from labui.runs import RunManager
from labui.store import Store


def _lan_addresses():
    """This machine's addresses, as other lab machines would type them."""
    out = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.168.1.1", 9))       # picks the LAN interface; sends nothing
        out.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return out


def run(cfg, source):
    store = Store(cfg.db_path)
    catalogue = Catalogue()
    refs = {}
    runs = RunManager(cfg, store, source, catalogue, lambda: refs.get("poller"),
                      lambda: refs.get("importer"))
    importer = Importer(cfg, store, source.lock_holder)
    refs["importer"] = importer
    runs.recover()
    poller = Poller(source, store, runs.mode)
    refs["poller"] = poller
    importer.start()
    app = App(cfg, store, poller, runs, catalogue, importer, source)
    try:
        server = Server((cfg.bind, cfg.port), make_handler(app))
    except OSError as e:
        sys.exit("ERROR: cannot listen on {}:{} ({}). Is the UI already running? Then just open "
                 "it in a browser.".format(cfg.bind, cfg.port, e))
    where = ["http://127.0.0.1:{}".format(cfg.port)]
    if cfg.bind in ("0.0.0.0", ""):
        where = ["http://{}:{}".format(a, cfg.port) for a in _lan_addresses()] + where
    print("Rubix lab UI{} - open {}".format(" (DEMO, fake fleet)" if cfg.demo else "",
                                            " or ".join(where)))
    print("Data: {}   Reports: {}".format(cfg.db_path, cfg.reports_dir))
    print("Ctrl+C stops the UI. A test run that is going keeps going, and the UI picks it up "
          "again when it starts.")
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nUI stopped.")
    finally:
        server.server_close()
