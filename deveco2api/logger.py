# -*- coding: utf-8 -*-
"""带颜色的日志格式化器。"""

import logging
import colorlog


def setup_logger(name: str, level: str | int = "INFO") -> logging.Logger:
    """创建 [时间][日志等级] 内容 格式的彩色日志器。"""
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(level)
    logger.propagate = False

    formatter = colorlog.ColoredFormatter(
        "%(log_color)s[%(asctime)s][%(levelname)s]%(reset)s %(message)s",
        datefmt="%H:%M:%S",
        log_colors={
            "DEBUG": "cyan",
            "INFO": "green",
            "WARNING": "yellow",
            "ERROR": "red",
            "CRITICAL": "bold_red",
        },
        secondary_log_colors={},
        style="%",
    )

    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    return logger
