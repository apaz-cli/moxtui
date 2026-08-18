# PyInstaller spec. Build ON the target OS -- PyInstaller cannot cross-compile,
# so a Windows .exe has to come from a Windows machine or a Windows CI runner.
#
#   pip install pyinstaller
#   pyinstaller moxfield.spec
#
# Output lands in dist/. See README for which of the two layouts to send.

from PyInstaller.utils.hooks import collect_all

# Textual ships .tcss stylesheets and resolves widgets lazily, so a plain
# dependency scan misses both. collect_all takes the data files too.
tcss, tbin, thidden = collect_all("textual")
# Optional: without it the card panel is text only, so a build that cannot find
# it is still a working build.
try:
    icss, ibin, ihidden = collect_all("textual_image")
except Exception:
    icss, ibin, ihidden = [], [], []

a = Analysis(
    ["moxfield.py"],
    datas=tcss + icss,
    binaries=tbin + ibin,
    # tui/builder are imported inside a function so the TUI stays optional;
    # name them so the freezer keeps them anyway.
    hiddenimports=thidden + ihidden + [
        "tui", "builder", "engine", "query", "api", "store",
        "cardlist", "cardpanel", "cardtypes", "colors", "deckview", "scryfall"],
    excludes=["tkinter", "unittest", "pydoc_data", "tree_sitter"],
    noarchive=False,
)
pyz = PYZ(a.pure)

ONEFILE = True   # False gives a folder: faster start, fewer antivirus false alarms

if ONEFILE:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, name="moxfield",
              console=True, upx=False, strip=False)
else:
    exe = EXE(pyz, a.scripts, name="moxfield", console=True, upx=False, strip=False)
    coll = COLLECT(exe, a.binaries, a.datas, name="moxfield", upx=False, strip=False)
