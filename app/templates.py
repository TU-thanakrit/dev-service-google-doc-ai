from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DocumentTemplate:
    description: str
    fields: tuple[str, ...]
    analysis_source: str | None = None
    aliases: tuple[str, ...] = ()


DOCUMENT_TYPES: dict[str, DocumentTemplate] = {
    "บิลเงินสด": DocumentTemplate(
        description="บิลเงินสดสำหรับค่าใช้จ่ายทั่วไป",
        fields=(
            "เลขที่เอกสาร",
            "วันที่",
            "ชื่อ Supplier",
            "ที่อยู่ Supplier",
            "สาขา Supplier",
            "ชื่อบริษัท",
            "สาขาบริษัท",
            "เลขผู้เสียภาษี",
            "ที่อยู่",
            "Amount",
            "Detail",
        ),
        analysis_source="Detail",
    ),
    "ใบรับรองใบแทนใบเสร็จ": DocumentTemplate(
        description="ใบรับรองแทนใบเสร็จสำหรับค่าใช้จ่ายทั่วไป",
        aliases=("ใบรับรองแทนใบเสร็จ",),
        fields=(
            "วันที่",
            "Detail",
            "Amount",
            "ชื่อบริษัท",
            "ชื่อผู้เบิก",
            "รหัสพนักงาน",
            "ตำแหน่ง",
            "ฝ่าย",
            "วันที่เริ่ม",
            "วันที่สิ้นสุด",
        ),
        analysis_source="Detail",
    ),
    "ทางด่วน": DocumentTemplate(
        description="ใบเสร็จหรือหลักฐานการชำระค่าทางด่วน",
        fields=("เลขที่เอกสาร", "วันที่", "Amount"),
    ),
    "ใบกำกับภาษี": DocumentTemplate(
        description="ใบกำกับภาษีสำหรับค่าใช้จ่ายทั่วไป",
        fields=(
            "เลขที่เอกสาร",
            "วันที่",
            "ชื่อ Supplier",
            "ที่อยู่ Supplier",
            "สาขา Supplier",
            "ชื่อบริษัท",
            "สาขาบริษัท",
            "เลขที่ผู้เสียภาษี",
            "ที่อยู่",
            "Amount",
            "จำนวน VAT",
            "Detail",
        ),
        analysis_source="Detail",
    ),
    "ใบเสร็จรับเงิน": DocumentTemplate(
        description="ใบเสร็จรับเงินสำหรับค่าใช้จ่ายทั่วไป",
        fields=(
            "เลขที่เอกสาร",
            "วันที่",
            "ชื่อ Supplier",
            "ที่อยู่ Supplier",
            "สาขา Supplier",
            "ชื่อบริษัท",
            "Amount",
            "จำนวน VAT",
            "Detail",
        ),
        analysis_source="Detail",
    ),
}


def expected_fields(document_type: str) -> tuple[str, ...]:
    return DOCUMENT_TYPES[document_type].fields
