"""provide upload cloud drive"""
import asyncio
import functools
import importlib
import inspect
import os
import re
from asyncio import subprocess
from subprocess import Popen
from typing import Callable
from zipfile import ZipFile

from loguru import logger

from utils import platform


# pylint: disable = R0902
class CloudDriveConfig:
    """Rclone Config"""

    def __init__(
        self,
        enable_upload_file: bool = False,
        before_upload_file_zip: bool = False,
        after_upload_file_delete: bool = True,
        rclone_path: str = os.path.join(
            os.path.abspath("."), "rclone", f"rclone{platform.get_exe_ext()}"
        ),
        remote_dir: str = "",
        upload_adapter: str = "rclone",
    ):
        self.enable_upload_file = enable_upload_file
        self.before_upload_file_zip = before_upload_file_zip
        self.after_upload_file_delete = after_upload_file_delete
        self.rclone_path = rclone_path
        self.remote_dir = remote_dir
        self.upload_adapter = upload_adapter
        self.dir_cache: dict = {}  # for remote mkdir
        self.total_upload_success_file_count = 0
        self.aligo = None

    def pre_run(self):
        """pre run init aligo"""
        if self.enable_upload_file and self.upload_adapter == "aligo":
            CloudDrive.init_upload_adapter(self)


class CloudDrive:
    """rclone support"""

    @staticmethod
    def init_upload_adapter(drive_config: CloudDriveConfig):
        """Initialize the upload adapter."""
        if drive_config.upload_adapter == "aligo":
            Aligo = importlib.import_module("aligo").Aligo
            drive_config.aligo = Aligo()

    @staticmethod
    def rclone_mkdir(drive_config: CloudDriveConfig, remote_dir: str):
        """mkdir in remote

        使用列表参数调用 rclone（不经 shell），remote_dir 可能包含来自
        Telegram 消息的频道标题/文件名，shell 拼接存在命令注入风险。
        """
        with Popen(
            [drive_config.rclone_path, "mkdir", remote_dir + "/"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        ) as p:
            out, _ = p.communicate(timeout=60)
            if p.returncode != 0:
                logger.warning(f"rclone mkdir failed: {out.decode(errors='replace')}")

    @staticmethod
    def aligo_mkdir(drive_config: CloudDriveConfig, remote_dir: str):
        """mkdir in remote by aligo"""
        if drive_config.aligo and not drive_config.aligo.get_folder_by_path(remote_dir):
            drive_config.aligo.create_folder(name=remote_dir, check_name_mode="refuse")

    @staticmethod
    def zip_file(local_file_path: str) -> str:
        """
        Zip local file

        若源文件本身是 .zip，直接返回原路径：否则 ZipFile(w) 会先截断
        与源文件同名的目标 zip，随后写入 0 字节，破坏原始下载内容。
        """
        if local_file_path.endswith(".zip"):
            logger.warning(
                f"{local_file_path} is already a zip, skip re-zip to avoid truncation"
            )
            return local_file_path

        file_path_without_extension = os.path.splitext(local_file_path)[0]
        zip_file_name = file_path_without_extension + ".zip"

        with ZipFile(zip_file_name, "w") as zip_writer:
            zip_writer.write(local_file_path)

        return zip_file_name

    @staticmethod
    def _remove_quiet(path: str):
        """删除文件；不存在时仅告警，避免误把上传成功当失败"""
        try:
            os.remove(path)
        except FileNotFoundError:
            logger.warning(f"remove {path}: file not found (already removed?)")
        except OSError as e:
            logger.warning(f"remove {path} failed: {e}")

    # pylint: disable = R0914
    @staticmethod
    async def rclone_upload_file(
        drive_config: CloudDriveConfig,
        save_path: str,
        local_file_path: str,
        progress_callback: Callable = None,
        progress_args: tuple = (),
    ) -> bool:
        """Use Rclone upload file"""
        upload_status: bool = False
        zip_file_path: str = ""
        try:
            remote_dir = (
                drive_config.remote_dir
                + "/"
                + os.path.dirname(local_file_path).replace(save_path, "")
                + "/"
            ).replace("\\", "/")

            if not drive_config.dir_cache.get(remote_dir):
                CloudDrive.rclone_mkdir(drive_config, remote_dir)
                drive_config.dir_cache[remote_dir] = True

            file_path = local_file_path
            if drive_config.before_upload_file_zip:
                zip_file_path = CloudDrive.zip_file(local_file_path)
                file_path = zip_file_path
            else:
                file_path = local_file_path

            # 使用列表参数调用 rclone（不经 shell）：file_path/remote_dir 可能
            # 包含来自 Telegram 消息的文件名/频道标题，shell 拼接（即使加引号）
            # 无法防御 $() / 反引号 命令注入
            proc = await asyncio.create_subprocess_exec(
                drive_config.rclone_path,
                "copy",
                file_path,
                remote_dir + "/",
                "--create-empty-src-dirs",
                "--ignore-existing",
                "--progress",
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            if proc.stdout:
                async for output in proc.stdout:
                    s = output.decode(errors="replace")
                    print(s)
                    pattern = (
                        r"Transferred: (.*?) / (.*?), (.*?)%, (.*?/s)?, ETA (.*?)$"
                    )
                    transferred_match = re.search(pattern, s)

                    if transferred_match and progress_callback:
                        func = functools.partial(
                            progress_callback,
                            transferred_match.group(1),
                            transferred_match.group(2),
                            transferred_match.group(3),
                            transferred_match.group(4),
                            transferred_match.group(5),
                            *progress_args,
                        )
                        if inspect.iscoroutinefunction(progress_callback):
                            await func()
                        else:
                            func()

            # 以退出码判定成功（文本匹配对不同 rclone 版本/语言不可靠）
            returncode = await proc.wait()
            if returncode == 0:
                logger.info(f"upload file {local_file_path} success")
                drive_config.total_upload_success_file_count += 1
                # 先清理 zip 临时副本（非源文件本身才删），再按配置删除源文件；
                # 删除失败不应把"已成功上传"误报为失败
                if zip_file_path and zip_file_path != local_file_path:
                    CloudDrive._remove_quiet(zip_file_path)
                if drive_config.after_upload_file_delete:
                    CloudDrive._remove_quiet(local_file_path)
                upload_status = True
            else:
                logger.warning(f"rclone upload failed, returncode={returncode}")
                # 上传失败保留源文件供重试，仅清理可复现的 zip 临时副本
                if zip_file_path and zip_file_path != local_file_path:
                    CloudDrive._remove_quiet(zip_file_path)
        except Exception as e:
            logger.error(f"{e.__class__} {e}")
            if zip_file_path and zip_file_path != local_file_path and os.path.exists(
                zip_file_path
            ):
                CloudDrive._remove_quiet(zip_file_path)
            return False

        return upload_status

    @staticmethod
    def aligo_upload_file(
        drive_config: CloudDriveConfig, save_path: str, local_file_path: str
    ):
        """aliyun upload file"""
        upload_status: bool = False
        zip_file_path: str = ""
        if not drive_config.aligo:
            logger.warning("please config aligo! see README.md")
            return False

        try:
            remote_dir = (
                drive_config.remote_dir
                + "/"
                + os.path.dirname(local_file_path).replace(save_path, "")
                + "/"
            ).replace("\\", "/")

            if not drive_config.dir_cache.get(remote_dir):
                CloudDrive.aligo_mkdir(drive_config, remote_dir)
                aligo_dir = drive_config.aligo.get_folder_by_path(remote_dir)
                if aligo_dir:
                    drive_config.dir_cache[remote_dir] = aligo_dir.file_id

            file_paths = []
            if drive_config.before_upload_file_zip:
                zip_file_path = CloudDrive.zip_file(local_file_path)
                file_paths.append(zip_file_path)
            else:
                file_paths.append(local_file_path)

            res = drive_config.aligo.upload_files(
                file_paths=file_paths,
                # mkdir/查找失败时目录未被缓存，兜底传到根目录而不是 KeyError
                parent_file_id=drive_config.dir_cache.get(remote_dir, "root"),
                check_name_mode="refuse",
            )

            if len(res) > 0:
                drive_config.total_upload_success_file_count += len(res)
                if drive_config.after_upload_file_delete:
                    CloudDrive._remove_quiet(local_file_path)

                if zip_file_path and zip_file_path != local_file_path:
                    CloudDrive._remove_quiet(zip_file_path)

                upload_status = True
            else:
                # 上传失败保留源文件供重试，仅清理可复现的 zip 临时副本
                if zip_file_path and zip_file_path != local_file_path:
                    CloudDrive._remove_quiet(zip_file_path)

        except Exception as e:
            logger.error(f"{e.__class__} {e}")
            if zip_file_path and zip_file_path != local_file_path and os.path.exists(
                zip_file_path
            ):
                CloudDrive._remove_quiet(zip_file_path)
            return False

        return upload_status

    @staticmethod
    async def upload_file(
        drive_config: CloudDriveConfig, save_path: str, local_file_path: str
    ) -> bool:
        """Upload file
        Parameters
        ----------
        drive_config: CloudDriveConfig
            see @CloudDriveConfig

        save_path: str
            Local file save path config

        local_file_path: str
            Local file path

        Returns
        -------
        bool
            True or False
        """
        if not drive_config.enable_upload_file:
            return False

        ret: bool = False
        if drive_config.upload_adapter == "rclone":
            ret = await CloudDrive.rclone_upload_file(
                drive_config, save_path, local_file_path
            )
        elif drive_config.upload_adapter == "aligo":
            ret = CloudDrive.aligo_upload_file(drive_config, save_path, local_file_path)

        return ret
