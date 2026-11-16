"""Build the PBIP source and verify metrics from deterministic synthetic data."""
import csv
import hashlib
import json
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PREFIX = "https://developer.microsoft.com/json-schemas/fabric/item/"
REPORT = "Inventory.Report"
MODEL = "Inventory.SemanticModel"
REPORT_SCHEMA = PREFIX + "report/definition/"


def write_json(path, data):
    file = ROOT / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


months = [f"2025-{i:02d}" for i in range(1, 7)]
regions = ["North", "West", "South", "East"]
products = [
    ("BAG-01", "Travel", 620), ("BAG-02", "Travel", 470),
    ("CABLE-03", "Electronics", 120), ("HUB-04", "Electronics", 350),
    ("BOTTLE-05", "Home", 180), ("LAMP-06", "Home", 540),
    ("CASE-07", "Electronics", 220), ("PILLOW-08", "Home", 310),
]
suppliers = ["Meridian Components", "Harbor Goods", "Ridgeway Supply"]
rng = random.Random(396)
inventory, demand, receipts = [], [], []
for month_no, month in enumerate(months):
    for region in regions:
        for product_no, (sku, _, cost) in enumerate(products):
            ordered = rng.randint(120, 470) + 9 * month_no
            shortage = 0 if rng.random() < 0.72 else rng.randint(3, 27)
            if region == "East" and product_no in (2, 3, 6) and month_no >= 3:
                shortage += rng.randint(18, 46)
                on_hand = rng.randint(0, 19)
            else:
                on_hand = rng.randint(40, 300)
            demand.append((month, region, sku, ordered, ordered - shortage))
            floor = max(20, round(ordered / 10))
            inventory.append((month, region, sku, on_hand, floor, on_hand * cost))
            supplier = suppliers[product_no % len(suppliers)]
            for delivery in range(3):
                po_units = ordered // 3 + rng.randint(0, 15)
                late = rng.random() < (0.29 if supplier == suppliers[0] else 0.11)
                short = rng.random() < (0.17 if supplier == suppliers[0] else 0.06)
                receipts.append((month, region, sku, supplier, po_units,
                                 po_units - (rng.randint(2, 16) if short else 0),
                                 int(late), int(not late and not short)))

tables = {
    "Month": ([("Month", "string")], [(m,) for m in months]),
    "Warehouse": ([("Region", "string")], [(r,) for r in regions]),
    "Product": ([("SKU", "string"), ("Category", "string"), ("Unit Cost", "int64")], products),
    "Supplier": ([("Supplier", "string")], [(s,) for s in suppliers]),
    "Inventory": ([("Month", "string"), ("Region", "string"), ("SKU", "string"),
                   ("On Hand", "int64"), ("Reorder Floor", "int64"), ("Stock Value", "int64")],
                  inventory),
    "Demand": ([("Month", "string"), ("Region", "string"), ("SKU", "string"),
                ("Ordered Units", "int64"), ("Filled Units", "int64")], demand),
    "Receipts": ([("Month", "string"), ("Region", "string"), ("SKU", "string"),
                  ("Supplier", "string"), ("PO Units", "int64"), ("Received Units", "int64"),
                  ("Late", "int64"), ("OTIF", "int64")], receipts),
}
formats = {"money": '"INR "#,0', "count": "#,0", "rate": "0.0%"}
measures = {
    "Inventory": [
        ("Inventory Value", "VAR AsOf = MAX(Inventory[Month]) RETURN CALCULATE(SUM(Inventory[Stock Value]), Inventory[Month] = AsOf)", "money"),
        ("On Hand Units", "VAR AsOf = MAX(Inventory[Month]) RETURN CALCULATE(SUM(Inventory[On Hand]), Inventory[Month] = AsOf)", "count"),
        ("Below Reorder", "VAR AsOf = MAX(Inventory[Month]) RETURN COUNTROWS(FILTER(Inventory, Inventory[Month] = AsOf && Inventory[On Hand] < Inventory[Reorder Floor])) + 0", "count"),
        ("Reorder Floor Units", "VAR AsOf = MAX(Inventory[Month]) RETURN CALCULATE(SUM(Inventory[Reorder Floor]), Inventory[Month] = AsOf)", "count"),
    ],
    "Demand": [
        ("Unit Fill Rate", "DIVIDE(SUM(Demand[Filled Units]), SUM(Demand[Ordered Units]))", "rate"),
        ("Unfilled Units", "SUM(Demand[Ordered Units]) - SUM(Demand[Filled Units])", "count"),
    ],
    "Receipts": [
        ("PO Lines", "COUNTROWS(Receipts)", "count"),
        ("Late PO Lines", "SUM(Receipts[Late])", "count"),
        ("Supplier OTIF", "DIVIDE(SUM(Receipts[OTIF]), [PO Lines])", "rate"),
        ("Short Receipt Units", "SUM(Receipts[PO Units]) - SUM(Receipts[Received Units])", "count"),
    ],
}


def model_definition():
    model_tables, links = [], []
    data_dir = ROOT / "data"
    data_dir.mkdir(exist_ok=True)
    for name, (schema, rows) in tables.items():
        with (data_dir / (name.lower() + ".csv")).open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([column for column, _ in schema])
            writer.writerows(rows)
        m_schema = ", ".join(
            '#"' + column + '" = ' + ("text" if dtype == "string" else "Int64.Type")
            for column, dtype in schema
        )
        m_rows = ",\n".join("{" + ", ".join(json.dumps(value) for value in row) + "}" for row in rows)
        model_tables.append({
            "name": name,
            "columns": [{"name": col, "sourceColumn": col, "dataType": dtype,
                         "summarizeBy": "none"} for col, dtype in schema],
            "measures": [{"name": metric, "expression": code, "formatString": formats[fmt]}
                         for metric, code, fmt in measures.get(name, [])],
            "partitions": [{"name": name, "mode": "import", "source": {"type": "m",
                           "expression": f"#table(type table [{m_schema}], {{\n{m_rows}\n}})"}}],
        })
        if name in ("Inventory", "Demand", "Receipts"):
            dims = [("Month", "Month"), ("Warehouse", "Region"), ("Product", "SKU")]
            if name == "Receipts":
                dims.append(("Supplier", "Supplier"))
            links.extend({"name": f"{name}_{dim}", "fromTable": name, "fromColumn": key,
                          "toTable": dim, "toColumn": key, "crossFilteringBehavior": "oneDirection"}
                         for dim, key in dims)
    return {"name": "Inventory", "compatibilityLevel": 1600,
            "model": {"culture": "en-US", "defaultPowerBIDataSourceVersion": "powerBI_V3",
                      "tables": model_tables, "relationships": links}}


def projection(table, column, kind="Measure"):
    return {"field": {kind: {"Expression": {"SourceRef": {"Entity": table}}, "Property": column}},
            "queryRef": f"{table}.{column}", "nativeQueryRef": column}


def col(table, column):
    return projection(table, column, "Column")


def measure(table, column):
    return projection(table, column)


def literal(value):
    return {"expr": {"Literal": {"Value": value}}}


def visual(page, index, kind, title, position, roles):
    name = hashlib.sha1(f"{page}:{index}".encode()).hexdigest()[:20]
    x, y, width, height = position
    content = {
        "$schema": REPORT_SCHEMA + "visualContainer/2.7.0/schema.json",
        "name": name,
        "position": {"x": x, "y": y, "z": index * 1000, "width": width, "height": height,
                     "tabOrder": index * 1000},
        "visual": {
            "visualType": kind,
            "query": {"queryState": {role: {"projections": fields} for role, fields in roles.items()}},
            "visualContainerObjects": {"title": [{"properties": {
                "show": literal("true"), "text": literal("'" + title + "'"),
                "fontSize": literal("13D"), "bold": literal("true")
            }}]},
            "drillFilterOtherVisuals": True,
        },
    }
    for fields in roles.values():
        for binding in fields:
            kind_key, expr = next(iter(binding["field"].items()))
            entity, prop = expr["Expression"]["SourceRef"]["Entity"], expr["Property"]
            choices = [c for c, _ in tables[entity][0]] if kind_key == "Column" else [
                m for m, _, _ in measures[entity]]
            assert prop in choices, (entity, prop)
    write_json(f"{REPORT}/definition/pages/{page}/visuals/{name}/visual.json", content)


def make_report():
    write_json("Inventory.pbip", {"version": "1.0", "artifacts": [{"report": {"path": REPORT}}],
                                  "settings": {"enableAutoRecovery": True}})
    write_json(f"{REPORT}/definition.pbir", {"version": "4.0",
               "datasetReference": {"byPath": {"path": "../" + MODEL}}})
    write_json(f"{MODEL}/definition.pbism", {"version": "1.0", "settings": {}})
    write_json(f"{MODEL}/model.bim", model_definition())
    write_json(f"{REPORT}/definition/version.json", {
        "$schema": REPORT_SCHEMA + "versionMetadata/1.0.0/schema.json", "version": "2.0.0"})
    write_json(f"{REPORT}/definition/report.json", {"$schema": REPORT_SCHEMA + "report/3.0.0/schema.json",
        "themeCollection": {"baseTheme": {"name": "CY24SU02", "type": "SharedResources",
            "reportVersionAtImport": {"visual": "1.8.147", "report": "2.0.147", "page": "1.3.147"}}}})
    pages = {"ReportSectionc02368366c67aa9282181010": "Inventory and service", "ReportSectionc02368366c67aa9282181020": "Supplier performance"}
    write_json(f"{REPORT}/definition/pages/pages.json", {
        "$schema": REPORT_SCHEMA + "pagesMetadata/1.0.0/schema.json", "pageOrder": list(pages),
        "activePageName": "ReportSectionc02368366c67aa9282181010"})
    for page, title in pages.items():
        write_json(f"{REPORT}/definition/pages/{page}/page.json", {
            "$schema": REPORT_SCHEMA + "page/2.0.0/schema.json", "name": page, "displayName": title,
            "displayOption": "FitToPage", "width": 1280, "height": 760})
        for index, (table, label) in enumerate(
            [("Month", "Month"), ("Warehouse", "Region"), ("Product", "Category")], start=1
        ):
            visual(page, index, "slicer", label, (24 + (index-1)*280, 16, 258, 100),
                   {"Values": [col(table, label)]})
    page = "ReportSectionc02368366c67aa9282181010"
    cards = [("Inventory", "Inventory Value"), ("Inventory", "Below Reorder"),
             ("Demand", "Unit Fill Rate"), ("Demand", "Unfilled Units")]
    for n, (table, metric) in enumerate(cards):
        visual(page, n + 4, "card", metric, (24 + n*312, 125, 294, 118),
               {"Values": [measure(table, metric)]})
    visual(page, 8, "lineChart", "Unit fill rate by month", (24, 259, 610, 226),
           {"Category": [col("Month", "Month")], "Y": [measure("Demand", "Unit Fill Rate")]})
    visual(page, 9, "clusteredBarChart", "SKU-site exceptions by region", (650, 259, 606, 226),
           {"Category": [col("Warehouse", "Region")], "Y": [measure("Inventory", "Below Reorder")]})
    visual(page, 10, "tableEx", "Replenishment review by warehouse and SKU", (24, 504, 1232, 230), {
        "Values": [col("Warehouse", "Region"), col("Product", "SKU"), col("Product", "Category"),
                   measure("Inventory", "On Hand Units"), measure("Inventory", "Reorder Floor Units"),
                   measure("Inventory", "Below Reorder"), measure("Inventory", "Inventory Value")]})
    page = "ReportSectionc02368366c67aa9282181020"
    for n, metric in enumerate(["Supplier OTIF", "PO Lines", "Late PO Lines", "Short Receipt Units"]):
        visual(page, n + 4, "card", metric, (24 + n*312, 125, 294, 118),
               {"Values": [measure("Receipts", metric)]})
    visual(page, 8, "lineChart", "Supplier OTIF by month", (24, 259, 610, 272), {
        "Category": [col("Month", "Month")], "Y": [measure("Receipts", "Supplier OTIF")],
        "Series": [col("Supplier", "Supplier")]})
    visual(page, 9, "clusteredBarChart", "Late PO lines by supplier", (650, 259, 606, 272), {
        "Category": [col("Supplier", "Supplier")], "Y": [measure("Receipts", "Late PO Lines")]})
    visual(page, 10, "tableEx", "Purchase order performance by supplier", (24, 550, 1232, 184), {
        "Values": [col("Supplier", "Supplier"), *(measure("Receipts", metric) for metric in
                   ("PO Lines", "Late PO Lines", "Supplier OTIF", "Short Receipt Units"))]})


def verify_data():
    assert len(inventory) == len(demand) == 192 and len(receipts) == 576
    assert len({row[:3] for row in inventory}) == len(inventory)
    assert all(0 <= row[4] <= row[3] for row in demand)
    assert all(0 <= row[5] <= row[4] and row[7] == int(row[6] == 0 and row[4] == row[5])
               for row in receipts)
    current = [row for row in inventory if row[0] == months[-1]]
    return {
        "latest_month": months[-1],
        "latest_inventory_value_inr": sum(row[5] for row in current),
        "latest_on_hand_units": sum(row[3] for row in current),
        "latest_below_reorder_sku_sites": sum(row[3] < row[4] for row in current),
        "period_fill_rate": sum(row[4] for row in demand) / sum(row[3] for row in demand),
        "period_unfilled_units": sum(row[3] - row[4] for row in demand),
        "period_supplier_otif": sum(row[7] for row in receipts) / len(receipts),
        "period_late_po_lines": sum(row[6] for row in receipts),
        "period_short_receipt_units": sum(row[4] - row[5] for row in receipts),
    }


if __name__ == "__main__":
    write_json("baseline_metrics.json", verify_data())
    make_report()
    print(json.dumps(verify_data(), indent=2))
