"""转换器集合。每种文件类型对应一个 converter。"""
from .base import BaseConverter, ConversionResult
from .pdf_converter import PdfConverter
from .docx_converter import DocxConverter
from .txt_converter import TxtConverter
from .csv_converter import CsvConverter
from .xlsx_converter import XlsxConverter
from .json_converter import JsonConverter
from .md_converter import MarkdownConverter
from .html_converter import HtmlConverter
from .image_converter import ImageConverter
# 新增
from .param_converter import ParamConverter
from .log_converter import LogConverter
from .rlog_converter import RlogConverter
from .tlog_converter import TlogConverter
from .wps_converter import WpsConverter
from .code_converter import ALL_CODE_KINDS, make_code_converter
from .exe_converter import ExeConverter
from .archive_converter import ALL_ARCHIVE_KINDS, make_archive_converter

__all__ = [
    "BaseConverter",
    "ConversionResult",
    "PdfConverter",
    "DocxConverter",
    "TxtConverter",
    "CsvConverter",
    "XlsxConverter",
    "JsonConverter",
    "MarkdownConverter",
    "HtmlConverter",
    "ImageConverter",
    # 新增
    "ParamConverter",
    "LogConverter",
    "RlogConverter",
    "TlogConverter",
    "WpsConverter",
    "ALL_CODE_KINDS",
    "make_code_converter",
    "ExeConverter",
    "ALL_ARCHIVE_KINDS",
    "make_archive_converter",
]
