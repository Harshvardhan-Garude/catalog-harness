"""Frozen human-authored synthetic listings and independently authored keys.

The records below are authored test cases, not generated model predictions.
Final records must not be used during development or policy selection. Public
files contain no labels; private files are restricted to the local evaluator.
"""

import json
from pathlib import Path


FIXTURE_VERSION = "semantic-v1"
SPLITS = ("development", "validation", "final")
SPLIT_COUNTS = {"development": 24, "validation": 12, "final": 24}


def _case(name, description, notes, categories, primary, required=(), optional=()):
    return {
        "fields": {"name": name, "description": description, "supplier_notes": notes},
        "labels": {
            "acceptable_categories": list(categories),
            "acceptable_primary_uses": list(primary),
            "required_secondary_uses": list(required),
            "optional_secondary_uses": list(optional),
        },
    }


# Each scenario and its answer key were authored together before live inference.
# No model outputs are loaded, consulted, or used to populate these constants.
_DEVELOPMENT = (
    _case("Stoneware morning cup", "Handled cup for drinking coffee or tea.",
          "Single vessel; blue glaze.", ("drinking_cup",), ("drink_beverages",)),
    _case("Workshop graduated beaker", "Use the marked cup to measure paint and resin by volume.",
          "Workshop measuring equipment.", ("measuring_cup",), ("measure_materials",)),
    _case("Mesh pencil cup", "Desktop organizer for pens, pencils, and rulers.",
          "Open mesh body with a flat base.", ("desk_organizer",), ("organize_desk_items",)),
    _case("Table-edge cup clamp", "Clamps to a desk and holds a separate drinking cup.",
          "This is a cup holder, not a vessel.", ("cup_holder",), ("hold_drinking_cup",)),
    _case("Ornamental silver chalice", "A decorative vessel for display on a mantel.",
          "Display prop only; not a drinking cup.", ("decorative_vessel",), ("display_decoration",)),
    _case("Jointed task lamp", "Adjustable desk lamp provides light over a work surface.",
          "Includes a shade and light source.", ("lighting",), ("provide_light",)),
    _case("Basil starter set", "Gardening kit with seeds, growing medium, and seed trays.",
          "Use the supplied parts to grow basil.", ("gardening_kit",), ("gardening",)),
    _case("Six-slot cable comb", "A silicone accessory that separates and organizes charging cables.",
          "Slots retain the cables along the desk edge.", ("cable_accessory",), ("organize_cables",)),
    _case("Snap-lid parts box", "Storage container for spare fasteners and small parts.",
          "Close the lid to keep stored items together.", ("storage_container",), ("store_items",)),
    _case("Arc 14", "Brushed cylinder, 90 mm tall.", "Finish: graphite.",
          ("unknown",), ("unknown",)),
    _case("Rail coffee cup", "This SKU is a drinking cup for beverages, not a cup holder.",
          "This exact SKU is only a rail-mounted holder for a separate cup, not a drinking vessel.",
          ("unknown",), ("unknown",)),
    _case("Aurora display tumbler", "A dual-function vessel for drinking beverages or displaying decoration.",
          "Drinking beverages and decorative display are both specified as main product functions.",
          ("drinking_cup", "decorative_vessel"), ("drink_beverages", "display_decoration"),
          optional=("drink_beverages", "display_decoration")),
    _case("Trail portion cup", "A graduated measuring cup, primarily for measuring ingredients at camp.",
          "It also serves as a drinking cup for beverages; this is an intended second function.",
          ("measuring_cup", "drinking_cup"), ("measure_materials",), required=("drink_beverages",)),
    _case("Pollinator sowing bundle", "Gardening kit containing wildflower seeds, soil plugs, and a sowing guide.",
          "Designed for growing a small pollinator garden.", ("gardening_kit",), ("gardening",)),
    _case("Stationery dock with cable channel", "Primarily a desk organizer for pens and sticky notes.",
          "Its rear channel is also designed to organize cables.",
          ("desk_organizer",), ("organize_desk_items",), required=("organize_cables",)),
    _case("Luminous display bowl", "An illuminated decorative vessel whose primary function is to provide light.",
          "The patterned bowl is also intended for decorative display.",
          ("lighting", "decorative_vessel"), ("provide_light",), required=("display_decoration",)),
    _case("Tall storage canister", "", "Purpose: storage container for household items.",
          ("storage_container",), ("store_items",)),
    _case("Cable connector sleeve", "An accessory that joins the ends of two cables.",
          "No cable routing or organizing function is specified.",
          ("cable_accessory",), ("unknown",)),
    _case("Armrest cup ring and pen bay", "A cup holder that primarily supports a separate drinking cup.",
          "The attached pen bay is a desk organizer for pens and pencils.",
          ("cup_holder",), ("hold_drinking_cup",), required=("organize_desk_items",)),
    _case("Replica festival goblet", "Decorative vessel only. It must not be used for drinking.",
          "Product correction: this exact goblet is exclusively a drinking cup, not a display prop.",
          ("unknown",), ("unknown",)),
    _case("Series L-9", "", "Product type: desk lamp. Intended function: provide light.",
          ("lighting",), ("provide_light",)),
    _case("R7", "", "", ("unknown",), ("unknown",)),
    _case("Balcony pea growing kit", "This gardening kit supplies pea seeds, coir discs, and a growing tray.",
          "Intended for growing peas on a balcony.", ("gardening_kit",), ("gardening",)),
    _case("Painted keepsake chest", "Decorative vessel primarily displayed as a painted ornament.",
          "The hinged cavity is also intended to store keepsakes.",
          ("decorative_vessel", "storage_container"), ("display_decoration",), required=("store_items",)),
)


_VALIDATION = (
    _case("Woven correspondence tray", "Desk organizer with sections for letters, notepads, and stationery.",
          "Place on a writing desk to arrange office supplies.",
          ("desk_organizer",), ("organize_desk_items",)),
    _case("Fertilizer dosing scoop", "A measuring cup with volume lines for measuring dry fertilizer.",
          "Do not infer food use from its scoop shape.", ("measuring_cup",), ("measure_materials",)),
    _case("Bicycle beverage bracket", "Handlebar-mounted cup holder supporting a separate drinking cup.",
          "The bracket has no vessel of its own.", ("cup_holder",), ("hold_drinking_cup",)),
    _case("Folding solar reading light", "A portable lamp intended to provide light for reading.",
          "Folds flat when stored.", ("lighting",), ("provide_light",)),
    _case("Tree grafting tool roll", "Task: join a scion to living rootstock when tending orchard trees. Supplied as a complete gardening kit.",
          "The grafting knife, binding tape, and collars are tools; no seeds, soil, or seedlings are supplied.",
          ("gardening_kit",), ("gardening",)),
    _case("Magnetic wire anchors", "Cable accessories that organize cables along a metal shelf.",
          "Each anchor retains a cable in its intended route.", ("cable_accessory",), ("organize_cables",)),
    _case("Divided pantry bin", "A storage container for packets and small pantry items.",
          "Removable dividers separate the stored items.", ("storage_container",), ("store_items",)),
    _case("Handblown memorial urn", "A decorative vessel for display; the cavity is permanently sealed.",
          "Intended use is decoration only.", ("decorative_vessel",), ("display_decoration",)),
    _case("Transit sip cup", "Drinking cup for beverages, supplied with a sipping lid.",
          "The lid belongs to this vessel; it is not a separate cup holder.",
          ("drinking_cup",), ("drink_beverages",)),
    _case("Object Q, small", "Smooth exterior; supplied individually.", "No intended use provided.",
          ("unknown",), ("unknown",)),
    _case("Studio volume vessel", "A measuring cup intended only for measuring clay slip, never for beverages.",
          "For this same item, use only as a drinking cup for beverages; volume marks are decorative.",
          ("unknown",), ("unknown",)),
    _case("Recipe sip-and-measure cup", "This dual-purpose cup is equally intended for measuring ingredients and drinking beverages.",
          "Measuring ingredients and drinking beverages are both main intended roles of the cup.",
          ("drinking_cup", "measuring_cup"), ("drink_beverages", "measure_materials"),
          optional=("drink_beverages", "measure_materials")),
)


_FINAL = (
    _case("Espresso tasting demitasse", "Small handled drinking cup used to drink espresso beverages.",
          "Serving vessel, sold without a saucer.", ("drinking_cup",), ("drink_beverages",)),
    _case("Graduated pigment dipper", "Measuring cup for measuring pigment powder before mixing.",
          "Volume graduations are functional.", ("measuring_cup",), ("measure_materials",)),
    _case("Monitor-top stationery shelf", "Desk organizer that arranges erasers, clips, and sticky-note pads above a monitor.",
          "Its compartments are intended for desktop supplies.", ("desk_organizer",), ("organize_desk_items",)),
    _case("Stroller beverage cradle", "Accessory cup holder that holds a separate drinking cup on a stroller frame.",
          "Beverage vessel not included.", ("cup_holder",), ("hold_drinking_cup",)),
    _case("Porcelain ceremonial ewer", "Decorative vessel for ornamental display in an alcove.",
          "Nonfunctional replica; no serving use intended.", ("decorative_vessel",), ("display_decoration",)),
    _case("Cabinet illumination strip", "Lighting strip intended to provide light inside a display cabinet.",
          "The listed item includes its light-emitting strip.", ("lighting",), ("provide_light",)),
    _case("Bonsai shaping set", "Contents card: branch-training wire; angled shears; root rake. Classification: gardening kit.",
          "Intended work is tending and shaping an existing bonsai tree. It does not include a plant.",
          ("gardening_kit",), ("gardening",)),
    _case("Braided cord sleeve", "Cable accessory for organizing several cables into one neat bundle.",
          "Wrap the sleeve around the cable group to retain its routing.", ("cable_accessory",), ("organize_cables",)),
    _case("Archive slide case", "Storage container for photographic slides.",
          "Numbered slots protect and store the slides.", ("storage_container",), ("store_items",)),
    _case("Milled oval assembly", "Anodized finish, rounded ends, two holes.", "Component number 61.",
          ("unknown",), ("unknown",)),
    _case("Museum urn C-2", "Decorative vessel for display only, with no lighting function.",
          "Contradictory specification for C-2: lighting only, not a decorative vessel.",
          ("unknown",), ("unknown",)),
    _case("Picnic marked tumbler", "Primarily a drinking cup for beverages; its graduations also measure liquid ingredients.",
          "Measuring ingredients is an explicit intended second function.",
          ("drinking_cup", "measuring_cup"), ("drink_beverages",), required=("measure_materials",)),
    _case("Valet pen-and-trinket bowl", "Primarily a desk organizer for pens and desktop supplies.",
          "Its separate covered compartment also stores small keepsakes.",
          ("desk_organizer", "storage_container"), ("organize_desk_items",), required=("store_items",)),
    _case("Decorative reading lantern", "A functional lamp and decorative vessel with equal intended uses: provide light and display decoration.",
          "Illumination and decorative display are each specified as a main function.",
          ("lighting", "decorative_vessel"), ("provide_light", "display_decoration"),
          optional=("provide_light", "display_decoration")),
    _case("Clip-on mug basket", "", "Product classification: cup holder. Holds a separate drinking cup.",
          ("cup_holder",), ("hold_drinking_cup",)),
    _case("Bench cup", "Dimensions supplied on request.", "The word cup is a shape label; no purpose is specified.",
          ("unknown",), ("unknown",)),
    _case("Axial cable adapter", "Cable accessory that changes a connector's orientation.",
          "It is not intended to bundle, retain, or organize cables.",
          ("cable_accessory",), ("unknown",)),
    _case("Counting vessel F", "This item is only a measuring cup for beads.",
          "This exact item is only a desktop pencil organizer; the measuring specification is incompatible.",
          ("unknown",), ("unknown",)),
    _case("Orchard irrigation service bundle", "Repairs drip-water lines serving orchard trees; this maintenance bundle is sold as a gardening kit.",
          "The intended gardening task is maintaining water delivery to growing trees, using replacement emitters and tubing connectors.",
          ("gardening_kit",), ("gardening",)),
    _case("Concert clip light", "A lamp designed to provide light on sheet music.", "Clips onto a music stand.",
          ("lighting",), ("provide_light",)),
    _case("Workshop washer jar", "A storage container with a screw lid for washers and nuts.",
          "Intended to store workshop hardware, with no measuring function.",
          ("storage_container",), ("store_items",)),
    _case("Sculptural bottle display", "Decorative vessel with a solid interior, intended for display as sculpture.",
          "The bottle outline does not imply a drinking or storage function.",
          ("decorative_vessel",), ("display_decoration",)),
    _case("Dockside rope-and-cable tidy", "Primarily an accessory for organizing electrical cables.",
          "Its attached lidded pocket also stores spare cable clips.",
          ("cable_accessory",), ("organize_cables",), required=("store_items",)),
    _case("Untitled sample 803", "", "Unclassified sample; function undetermined.",
          ("unknown",), ("unknown",)),
)

_CASES = {"development": _DEVELOPMENT, "validation": _VALIDATION, "final": _FINAL}


def authored_split(split):
    """Return fresh public records and private keys; never accepts predictions."""
    if split not in SPLITS:
        raise ValueError("Invalid enrichment fixture split")
    prefix = {"development": "dev", "validation": "val", "final": "final"}[split]
    records, labels = [], []
    for index, case in enumerate(_CASES[split], 1):
        source_id = "sem-v1-{}-{:03d}".format(prefix, index)
        fields = dict(case["fields"])
        records.append({"source_record_id": source_id, "fields": fields,
                        "normalized_product": {"product_id": "P-" + source_id,
                                               "name": fields["name"], "weight_g": 250.0}})
        labels.append({"source_record_id": source_id,
                       **{key: list(value) for key, value in case["labels"].items()}})
    return records, labels


def generate(root=Path("data/enrichment-v1")):
    """Write immutable split files; refuse existing content that differs."""
    root = Path(root)
    public_dir = root / "agent_visible"
    private_dir = root / "evaluator_only"
    documents = {}
    for split in SPLITS:
        records, labels = authored_split(split)
        if len(records) != SPLIT_COUNTS[split]:
            raise ValueError("Frozen fixture size mismatch")
        for folder, rows in ((public_dir, records), (private_dir, labels)):
            documents[folder / (split + ".jsonl")] = "".join(
                json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n" for row in rows)
    # Check the complete generation before writing any new file.
    for path, content in documents.items():
        if path.exists() and path.read_text(encoding="utf-8") != content:
            raise ValueError("Frozen enrichment fixture differs; do not overwrite")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    for folder in (public_dir, private_dir):
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        folder.chmod(0o700)
    for path, content in documents.items():
        if not path.exists():
            # Exclusive creation plus restricted mode avoids a permissive window.
            with path.open("x", encoding="utf-8") as handle:
                path.chmod(0o600)
                handle.write(content)
        path.chmod(0o600)
    return dict(SPLIT_COUNTS)


def _load(root, folder, split):
    if split not in SPLITS:
        raise ValueError("Invalid enrichment fixture split")
    with (Path(root) / folder / (split + ".jsonl")).open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def load_public(root, split):
    return _load(root, "agent_visible", split)


def load_labels(root, split):
    """Evaluator-only API. Never give these return values to either model."""
    return _load(root, "evaluator_only", split)
