# PyInstaller recipe for the macOS app (used by build_app.command).
#
# The PySide6 hooks bring in more of Qt than this app uses (QML/Quick, PDF,
# network/TLS, virtual keyboard, OpenGL, SVG, translations ...). Those parts are
# left out here. Set BBR_FULL_BUILD=1 to build without trimming (fallback).
import os
import re

APP = "Blender Batch Renderer"
FULL = os.environ.get("BBR_FULL_BUILD") == "1"

EXCLUDES = ["tkinter", "unittest", "pydoc", "doctest", "pdb", "lib2to3", "test"]
if not FULL:
    EXCLUDES += ["PySide6." + m for m in (
        "QtNetwork", "QtQml", "QtQuick", "QtQuickWidgets", "QtQuickControls2", "QtPdf", "QtPdfWidgets",
        "QtDBus", "QtOpenGL", "QtOpenGLWidgets", "QtCharts", "QtSvg", "QtSvgWidgets", "QtPrintSupport",
        "QtSql", "QtXml", "QtTest", "QtConcurrent", "QtMultimedia", "QtWebEngineCore", "QtWebEngineWidgets",
        "Qt3DCore", "QtDesigner", "QtUiTools", "QtHelp", "QtBluetooth", "QtPositioning", "QtSerialPort")]

# Qt libraries / plugins that are never needed by this app.
# (QtDBus stays: on macOS QtGui itself links against it.)
DROP = re.compile(
    r"Qt(Qml\w*|Quick\w*|VirtualKeyboard\w*|Pdf\w*|Network|OpenGL\w*|Charts|Svg\w*|Multimedia\w*|"
    r"WebEngine\w*|Designer\w*|Sql|Test|PrintSupport|Concurrent)(\.framework|\.abi3|\b)"
    r"|/translations/|platforminputcontexts|/tls/|networkinformation|libqtuiotouchplugin"
    r"|libqminimal|libqoffscreen|libqpdf|libqsvg|/qml/")


def keep(entry):
    if FULL:
        return True
    dest = entry[0].replace("\\", "/")
    return not DROP.search(dest)


a = Analysis(
    ["bbr_app.py"],
    pathex=["."],
    datas=[("bbr/blender_scripts", "bbr/blender_scripts"), ("bbr/assets", "bbr/assets"),
           ("blender_addon", "blender_addon")]
          + ([("docs/Blender_Batch_Renderer_Documentation.pdf", "docs")]
             if os.path.exists("docs/Blender_Batch_Renderer_Documentation.pdf") else []),
    excludes=EXCLUDES,
    noarchive=False,
)
dropped = [b[0] for b in list(a.binaries) + list(a.datas) if not keep(b)]
a.binaries = [b for b in a.binaries if keep(b)]
a.datas = [d for d in a.datas if keep(d)]
print(f"bbr.spec: left out {len(dropped)} unused Qt files")

pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name=APP, console=False,
          icon="bbr/assets/icon.icns", strip=False, upx=False)
coll = COLLECT(exe, a.binaries, a.datas, name=APP, strip=False, upx=False)
app = BUNDLE(
    coll,
    name=f"{APP}.app",
    icon="bbr/assets/icon.icns",
    bundle_identifier="io.github.germancgartist.blenderbatchrenderer",
    info_plist={
        "CFBundleName": APP,
        "CFBundleDisplayName": APP,
        "CFBundleShortVersionString": "0.4.0",
        # double-clicking a .bbrqueue file opens it in the app
        "CFBundleDocumentTypes": [{
            "CFBundleTypeName": "Batch Renderer Queue",
            "CFBundleTypeRole": "Editor",
            "LSHandlerRank": "Owner",
            "CFBundleTypeIconFile": "icon.icns",
            "LSItemContentTypes": ["io.github.germancgartist.blenderbatchrenderer.queue"],
        }],
        "UTExportedTypeDeclarations": [{
            "UTTypeIdentifier": "io.github.germancgartist.blenderbatchrenderer.queue",
            "UTTypeDescription": "Batch Renderer Queue",
            "UTTypeConformsTo": ["public.json", "public.data"],
            "UTTypeIconFile": "icon.icns",
            "UTTypeTagSpecification": {"public.filename-extension": ["bbrqueue"]},
        }],
        "NSHighResolutionCapable": True,
        "LSApplicationCategoryType": "public.app-category.graphics-design",
    },
)
