
import frappe
from frappe.utils import flt, formatdate
import re


# ============================================================
# FBR CONFIGURATION
# ============================================================

STANDARD_RATE_SALE_TYPE = "Goods at standard rate (default)"
STANDARD_RATE_SCENARIO_IDS = {"SN001", "SN002", "SN026"}

REDUCED_RATE_SALE_TYPE = "Goods at Reduced Rate"
REDUCED_RATE_SCENARIO_IDS = {"SN005", "SN028"}

THIRD_SCHEDULE_SALE_TYPE = "3rd Schedule Goods"
THIRD_SCHEDULE_SCENARIO_IDS = {"SN008", "SN027"}

SANDBOX_MODE_LABEL = "Sandbox Testing"
PRODUCTION_MODE_LABEL = "Production"

MODE_SANDBOX = "sandbox"
MODE_PRODUCTION = "production"

FBR_MODE_MAP = {
    SANDBOX_MODE_LABEL.casefold(): MODE_SANDBOX,
    PRODUCTION_MODE_LABEL.casefold(): MODE_PRODUCTION,
}


# ============================================================
# FBR MODE VALIDATION
# ============================================================

def _mode_error_message(company, configured_mode=None):
    configured = (configured_mode or "").strip()
    configured_display = configured or "blank"
    company_label = company or "(no company)"

    return (
        f"Invalid FBR Mode on Company '{company_label}'. "
        f"Current value: '{configured_display}'. "
        f"Open Company -> FBR tab and set Mode to "
        f"'{SANDBOX_MODE_LABEL}' or '{PRODUCTION_MODE_LABEL}'."
    )


def _resolve_fbr_mode_or_throw(company=None):
    from fbr_e_invoicing.utils import _get_fbr_mode

    configured_mode = _get_fbr_mode(company)
    normalized_mode = (configured_mode or "").strip().casefold()

    mode = FBR_MODE_MAP.get(normalized_mode)

    if not mode:
        raise frappe.ValidationError(
            _mode_error_message(company, configured_mode)
        )

    return mode


# ============================================================
# TAX ID NORMALIZATION
# ============================================================

def normalise_cnic(value):
    if not value:
        return ""

    return re.sub(r"\D", "", str(value)).strip()


def _get_customer_tax_data(customer_name):
    if not customer_name:
        return {}

    return (
        frappe.db.get_value(
            "Customer",
            customer_name,
            ["tax_id", "nic", "ntn", "customer_name"],
            as_dict=True
        )
        or {}
    )


def _resolve_customer_tax_id(customer_tax_data):
    buyer_tax_id = customer_tax_data.get("tax_id")

    if not buyer_tax_id and customer_tax_data.get("nic"):
        buyer_tax_id = normalise_cnic(
            customer_tax_data.get("nic")
        )

    if not buyer_tax_id and customer_tax_data.get("ntn"):
        buyer_tax_id = customer_tax_data.get("ntn")

    return buyer_tax_id or ""


# ============================================================
# SALES INVOICE PAYLOAD
# ============================================================

@frappe.whitelist()
def build_fbr_payload(sales_invoice_name):

    doc = frappe.get_doc(
        "Sales Invoice",
        sales_invoice_name
    )

    invoice_type = (
        "Debit Note"
        if getattr(doc, "is_debit_note", 0)
        else "Sale Invoice"
    )

    party_fields = _get_sales_party_fields(doc)

    return _build_invoice_payload(
        doc,
        invoice_type,
        party_fields
    )


# ============================================================
# POS INVOICE PAYLOAD
# ============================================================

@frappe.whitelist()
def build_pos_fbr_payload(pos_invoice_name):

    doc = frappe.get_doc(
        "POS Invoice",
        pos_invoice_name
    )

    party_fields = _get_pos_party_fields(doc)

    return _build_invoice_payload(
        doc,
        "Sale Invoice",
        party_fields
    )


# ============================================================
# SALES INVOICE PARTY DETAILS
# ============================================================

def _get_sales_party_fields(doc):

    customer_tax_data = _get_customer_tax_data(
        doc.customer
    )

    buyer_tax_id = _resolve_customer_tax_id(
        customer_tax_data
    )

    seller_tax_id = (
        frappe.db.get_value(
            "Company",
            doc.company,
            "tax_id"
        )
        if doc.company else None
    )

    return {
        "sellerNTNCNIC": seller_tax_id or "",
        "sellerBusinessName": doc.company or "",
        "sellerProvince": doc.custom_province or "",
        "sellerAddress": (
            _get_party_address_text(
                "Company",
                doc.company
            ) or ""
        ),

        "buyerNTNCNIC": buyer_tax_id or "",

        "buyerBusinessName": (
            customer_tax_data.get("customer_name")
            or doc.customer_name
            or ""
        ),

        "buyerProvince": doc.tax_category or "",

        "buyerAddress": (
            _get_party_address_text(
                "Customer",
                doc.customer
            ) or ""
        ),

        "buyerRegistrationType": (
            "Registered"
            if buyer_tax_id
            else "Unregistered"
        ),
    }


# ============================================================
# POS PARTY DETAILS
# ============================================================

def _get_pos_party_fields(doc):

    seller_name = (
        frappe.db.get_value(
            "POS Profile",
            doc.pos_profile,
            "company"
        )
        if doc.pos_profile
        else None
    )

    seller_tax_id = (
        frappe.db.get_value(
            "Company",
            seller_name,
            "tax_id"
        )
        if seller_name
        else None
    )

    customer_tax_data = _get_customer_tax_data(
        doc.customer
    )

    buyer_tax_id = _resolve_customer_tax_id(
        customer_tax_data
    )

    return {
        "sellerNTNCNIC": seller_tax_id or "",
        "sellerBusinessName": seller_name or "",
        "sellerProvince": doc.custom_province or "",

        "sellerAddress": (
            _get_party_address_text(
                "Company",
                seller_name
            ) or ""
        ),

        "buyerNTNCNIC": buyer_tax_id or "",

        "buyerBusinessName": (
            customer_tax_data.get("customer_name")
            or doc.customer_name
            or ""
        ),

        "buyerProvince": doc.tax_category or "",

        "buyerAddress": (
            _get_party_address_text(
                "Customer",
                doc.customer
            ) or ""
        ),

        "buyerRegistrationType": (
            "Registered"
            if buyer_tax_id
            else "Unregistered"
        ),
    }


# ============================================================
# MAIN PAYLOAD GENERATOR
# ============================================================

def _build_invoice_payload(doc, invoice_type, party_fields):

    first_sale_type = (
        doc.items[0].custom_sale_type
        if doc.items
        else ""
    )

    fbr_mode = _resolve_fbr_mode_or_throw(
        getattr(doc, "company", None)
    )

    # --------------------------------------------------------
    # INVOICE LEVEL DETAILS
    # --------------------------------------------------------

    payload = {
        "invoiceType": invoice_type,

        "invoiceDate": formatdate(
            doc.posting_date,
            "yyyy-mm-dd"
        ),

        **party_fields,

        "invoiceRefNo": "",

        "items": [],
    }

    # --------------------------------------------------------
    # SANDBOX SCENARIO
    # --------------------------------------------------------

    if fbr_mode == MODE_SANDBOX:

        scenario_id = get_scenario_id(
            first_sale_type
        )

        if not scenario_id:
            raise frappe.ValidationError(
                "Unable to resolve Scenario ID for Sandbox Testing mode. "
                "Please configure a valid FBR Sale Type."
            )

        payload["scenarioId"] = scenario_id

    # --------------------------------------------------------
    # ITEM MAPPING
    # --------------------------------------------------------

    for row in doc.items or []:

        # 1. TAX RATE
        tax_rate = _first_item_tax_rate(
            row.item_tax_template
        )

        # 2. QUANTITY
        quantity = flt(row.qty, 6)

        # 3. UNIT RATE (kept separate for reference)
        unit_rate = flt(row.rate, 2)

        # 4. ITEM NET AMOUNT
        # IMPORTANT:
        # net_amount is the full line amount after
        # applicable item-level discounts.
        # Do NOT use row.rate here.
        value_excl_st = flt(
            row.net_amount,
            2
        )

        # 5. GST AMOUNT
        sales_tax_applicable = round(
            (tax_rate * value_excl_st) / 100.0,
            2
        )

        # 6. TOTAL INCLUDING GST
        total_values = round(
            value_excl_st + sales_tax_applicable,
            2
        )

        # 7. SALE TYPE
        raw_sale_type = str(
            row.custom_sale_type or ""
        ).strip()

        scenario_id = get_scenario_id(
            raw_sale_type
        )

        payload_sale_type = map_sale_type_for_payload(
            raw_sale_type,
            scenario_id
        )

        # ----------------------------------------------------
        # BASIC VALIDATION
        # ----------------------------------------------------

        if quantity <= 0:
            raise frappe.ValidationError(
                f"Invalid quantity for item {row.item_code}: {quantity}"
            )

        if value_excl_st < 0:
            raise frappe.ValidationError(
                f"Invalid net amount for item {row.item_code}"
            )

        if not row.custom_hs_code:
            frappe.logger().warning(
                f"Missing HS Code for item {row.item_code}"
            )

        # ----------------------------------------------------
        # FBR ITEM ENTRY
        # ----------------------------------------------------

        item_entry = {

            "hsCode": (
                row.custom_hs_code or ""
            ),

            "productDescription": (
                row.description
                or row.item_name
                or ""
            ),

            "rate": format_rate(
                tax_rate
            ),

            "uoM": str(
                row.stock_uom
                or row.uom
                or ""
            ).strip(),

            "quantity": quantity,

            "totalValues": total_values,

            "valueSalesExcludingST": value_excl_st,

            "fixedNotifiedValueOrRetailPrice": 0.00,

            "salesTaxApplicable": sales_tax_applicable,

            "salesTaxWithheldAtSource": 0.00,

            "extraTax": 0.00,

            "furtherTax": 0.00,

            "sroScheduleNo": "",

            "fedPayable": 0.00,

            "discount": abs(
                flt(row.discount_amount or 0.0, 2)
            ),

            "saleType": payload_sale_type,

            "sroItemSerialNo": "",
        }

        payload["items"].append(
            item_entry
        )

        # ----------------------------------------------------
        # DEBUG LOG
        # ----------------------------------------------------

        frappe.logger().info(
            "FBR ITEM PAYLOAD | "
            f"Invoice: {doc.name} | "
            f"Item: {row.item_code} | "
            f"Qty: {quantity} | "
            f"Unit Rate: {unit_rate} | "
            f"Net Amount: {value_excl_st} | "
            f"GST Rate: {tax_rate} | "
            f"GST Amount: {sales_tax_applicable} | "
            f"Total: {total_values}"
        )

    return payload


# ============================================================
# RATE FORMATTER
# ============================================================

def format_rate(tax_rate):

    value = flt(tax_rate)

    if value.is_integer():
        return f"{int(value)}%"

    return f"{value}%"


# ============================================================
# FBR SCENARIO ID
# ============================================================

def get_scenario_id(sale_type):

    normalized_sale_type = (
        sale_type or ""
    ).strip()

    if not normalized_sale_type:
        return ""

    try:

        scenario_id = frappe.db.get_value(
            "FBR Sale Type",
            {
                "name": normalized_sale_type
            },
            "scenario_id"
        )

        return scenario_id or ""

    except Exception:

        return ""


# ============================================================
# SALE TYPE MAPPING
# ============================================================

def map_sale_type_for_payload(
    sale_type,
    scenario_id
):

    normalized_sale_type = (
        sale_type or ""
    ).strip()

    if not normalized_sale_type:
        return ""

    if scenario_id in STANDARD_RATE_SCENARIO_IDS:
        return STANDARD_RATE_SALE_TYPE

    if scenario_id in REDUCED_RATE_SCENARIO_IDS:
        return REDUCED_RATE_SALE_TYPE

    if scenario_id in THIRD_SCHEDULE_SCENARIO_IDS:
        return THIRD_SCHEDULE_SALE_TYPE

    return normalized_sale_type


# ============================================================
# PARTY ADDRESS FETCHING
# ============================================================

def _get_party_address_text(
    link_doctype,
    link_name
):

    if not (link_doctype and link_name):
        return ""

    address_names = frappe.get_all(
        "Dynamic Link",
        filters={
            "parenttype": "Address",
            "link_doctype": link_doctype,
            "link_name": link_name,
        },
        pluck="parent",
        limit=50
    )

    if not address_names:
        return ""

    addresses = frappe.get_all(
        "Address",
        filters={
            "name": ["in", address_names],
            "disabled": 0,
        },
        fields=[
            "name",
            "address_line1",
            "address_line2",
            "city",
            "state",
            "pincode",
            "is_primary_address",
            "creation",
        ],
        order_by="is_primary_address desc, creation desc",
        limit=1
    )

    if not addresses:
        return ""

    address = addresses[0]

    parts = [
        address.get("address_line1"),
        address.get("address_line2"),
        address.get("city"),
        address.get("state"),
        address.get("pincode"),
    ]

    return ", ".join(
        [part for part in parts if part]
    )


# ============================================================
# ITEM TAX TEMPLATE RATE
# ============================================================

def _first_item_tax_rate(
    item_tax_template_name
):

    if not item_tax_template_name:
        return 0.0

    try:

        item_tax_template = frappe.get_doc(
            "Item Tax Template",
            item_tax_template_name
        )

        if (
            getattr(item_tax_template, "taxes", None)
            and len(item_tax_template.taxes) > 0
        ):

            return flt(
                item_tax_template.taxes[0].tax_rate
            )

    except Exception:

        frappe.logger().exception(
            "Unable to fetch Item Tax Template rate: "
            f"{item_tax_template_name}"
        )

    return 0.0
