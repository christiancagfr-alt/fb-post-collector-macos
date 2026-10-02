"""Reviewed component versions and SHA256 digests (2026-10-02).

GitHub release digests / official Python download / pinned tessdata git blobs.
Updating an executable requires updating both its versioned URL and digest.
"""
YT_DLP_URL = "https://github.com/yt-dlp/yt-dlp/releases/download/2026.08.19/yt-dlp.exe"
FFMPEG_URLS = ["https://github.com/GyanD/codexffmpeg/releases/download/8.1.2/ffmpeg-8.1.2-essentials_build.zip"]
TESSERACT_SETUP_URLS = ["https://github.com/tesseract-ocr/tesseract/releases/download/5.5.3/tesseract-ocr-w64-setup-5.5.3.20260724.exe"]
PYTHON_SETUP_URL = "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"
PIP_REQUIREMENT = "https://files.pythonhosted.org/packages/f3/6e/1736e5b4ae2b778ef2f81c47d797de9f891d4d8acb047a24ca37a60294dd/pip-26.2.1-py3-none-any.whl#sha256=71138adf1f4ca900cdb7d289c21b7494329f2332b6d85f0e1c42108c0384ed3e"
WHISPER_REQUIREMENT = "https://files.pythonhosted.org/packages/35/8e/d36f8880bcf18ec026a55807d02fe4c7357da9f25aebd92f85178000c0dc/openai_whisper-20250625.tar.gz#sha256=37a91a3921809d9f44748ffc73c0a55c9f366c85a3ef5c2ae0cc09540432eb96"
TESSERACT_MANUAL_LINKS = [
    {"label": "官方 Windows 64 位安装包", "url": TESSERACT_SETUP_URLS[0]},
    {"label": "官方 5.5.3 下载页面", "url": "https://github.com/tesseract-ocr/tesseract/releases/tag/5.5.3"},
]
TESSDATA_COMMIT = "87416418657359cb625c412a48b6e1d6d41c29bd"
TESSDATA_HASHES = {
    "eng": "7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2",
    "por": "c4932b937207a9514b7514d518b931a99938c02a28a5a5a553f8599ed58b7deb",
    "ara": "e3206d3dc87fd50c24a0fb9f01838615911d25168f4e64415244b67d2bb3e729",
    "chi_sim": "a5fcb6f0db1e1d6d8522f39db4e848f05984669172e584e8d76b6b3141e1f730",
    "swa": "395439d1ec308535066cbaea9b15e0e4cc81f8609170af76ab7e7e8d3ec42f3e",
    "fra": "ced037562e8c80c13122dece28dd477d399af80911a28791a66a63ac1e3445ca",
    "Latin": "6dbdaf8ecc6c40f025c2648bf3b3f3fbffe073e1fd2df2047fde2e2b2f020d53",
}
SHA256 = {
    YT_DLP_URL: "66674953fe251b89f4d08c5f0e35e0728679bd67ab3d7d05c0562af101dd3e7a",
    FFMPEG_URLS[0]: "db580001caa24ac104c8cb856cd113a87b0a443f7bdf47d8c12b1d740584a2ec",
    TESSERACT_SETUP_URLS[0]: "bee9e3434bd94fd65387d9be28cd467a41f61b1275383b55b0f59a1331270ae4",
    PYTHON_SETUP_URL: "67b5635e80ea51072b87941312d00ec8927c4db9ba18938f7ad2d27b328b95fb",
}
TESSDATA_URLS = {}
for language, digest in TESSDATA_HASHES.items():
    filename = "script/Latin" if language == "Latin" else language
    urls = [f"https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/{TESSDATA_COMMIT}/{filename}.traineddata",
            f"https://github.com/tesseract-ocr/tessdata_fast/raw/{TESSDATA_COMMIT}/{filename}.traineddata"]
    TESSDATA_URLS[language] = urls
    SHA256.update({url: digest for url in urls})
