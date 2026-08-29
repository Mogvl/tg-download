"""Utility functions to handle downloaded files."""
import glob
import os
import pathlib
from hashlib import md5


def _md5_of_file(path: str) -> str:
    """分块计算文件 md5，避免大文件（多 GB 视频）整体读入内存"""
    h = md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def get_next_name(file_path: str) -> str:
    """
    Get next available name to download file.

    Parameters
    ----------
    file_path: str
        Absolute path of the file for which next available name to
        be generated.

    Returns
    -------
    str
        Absolute path of the next available name for the file.
    """
    posix_path = pathlib.Path(file_path)
    counter: int = 1
    new_file_name: str = os.path.join("{0}", "{1}-copy{2}{3}")
    while os.path.isfile(
        new_file_name.format(
            posix_path.parent,
            posix_path.stem,
            counter,
            "".join(posix_path.suffixes),
        )
    ):
        counter += 1
    return new_file_name.format(
        posix_path.parent,
        posix_path.stem,
        counter,
        "".join(posix_path.suffixes),
    )


def manage_duplicate_file(file_path: str):
    """
    Check if a file is duplicate.

    Compare the md5 of files with copy name pattern
    and remove if the md5 hash is same.

    Parameters
    ----------
    file_path: str
        Absolute path of the file for which duplicates needs to
        be managed.

    Returns
    -------
    str
        Absolute path of the duplicate managed file.
    """
    # pylint: disable = R1732
    posix_path = pathlib.Path(file_path)
    file_base_name: str = "".join(posix_path.stem.split("-copy")[0])
    name_pattern: str = f"{posix_path.parent}/{file_base_name}*"
    # 文件名可能含 * ? [ ] 等 glob 元字符：先对基础名与父目录整体 glob.escape，
    # 再手动拼上我们自己的 * 通配；只转义 [] 会让通配符注入误匹配/删错文件
    escaped_prefix = glob.escape(f"{posix_path.parent}/{file_base_name}")
    old_files: list = glob.glob(f"{escaped_prefix}*")
    if file_path in old_files:
        old_files.remove(file_path)
    current_file_md5: str = _md5_of_file(file_path)
    for old_file_path in old_files:
        old_file_md5: str = _md5_of_file(old_file_path)
        if current_file_md5 == old_file_md5:
            os.remove(file_path)
            return old_file_path
    return file_path
