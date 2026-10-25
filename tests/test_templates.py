from app.templates import DOCUMENT_TYPES, expected_fields


def test_registry_contains_exactly_five_templates():
    assert set(DOCUMENT_TYPES) == {
        "บิลเงินสด",
        "ใบรับรองใบแทนใบเสร็จ",
        "ทางด่วน",
        "ใบกำกับภาษี",
        "ใบเสร็จรับเงิน",
    }


def test_each_template_exposes_exact_specification_keys():
    assert expected_fields("บิลเงินสด") == (
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
    )
    assert expected_fields("ใบรับรองใบแทนใบเสร็จ") == (
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
    )
    assert expected_fields("ทางด่วน") == ("เลขที่เอกสาร", "วันที่", "Amount")
    assert expected_fields("ใบกำกับภาษี") == (
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
    )
    assert expected_fields("ใบเสร็จรับเงิน") == (
        "เลขที่เอกสาร",
        "วันที่",
        "ชื่อ Supplier",
        "ที่อยู่ Supplier",
        "สาขา Supplier",
        "ชื่อบริษัท",
        "Amount",
        "จำนวน VAT",
        "Detail",
    )


def test_only_detail_templates_request_expense_analysis():
    for _name, template in DOCUMENT_TYPES.items():
        if "Detail" in template.fields:
            assert template.analysis_source == "Detail"
        else:
            assert template.analysis_source is None
