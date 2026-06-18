"""
Shared consumer-category keyword map used across all scan services.

This is the union of the keyword lists from delaware.py, newswire.py, and
producthunt.py — delaware.py's comprehensive list forms the base, with
unique terms from the other two services merged in.
"""

CATEGORY_KEYWORDS = {
    "CPG/Food/Drink": [
        "food", "foods", "beverage", "beverages", "drink", "drinks", "bev",
        "brew", "brewery", "brewing", "coffee", "tea", "juice", "bar", "bars",
        "snack", "snacks", "bite", "bites", "eats", "kitchen", "farms", "farm",
        "harvest", "organic", "fresh", "cafe", "bakery", "bake", "wine", "winery",
        "spirits", "distillery", "water", "soda", "nutrition", "nutritional",
        "provisions", "pantry", "table", "grains", "cacao", "chocolate",
        "condiment", "sauce", "oil", "oils", "dairy", "plant-based", "vegan",
        "keto", "paleo", "gut", "protein", "meal", "meals", "chew", "chews",
        "crisp", "crunch", "fizz", "sparkling", "kombucha", "matcha", "mushroom",
        "adaptogen", "collagen", "prebiotic", "probiotic", "ferment", "fermented",
        "broth", "bouillon", "grain", "seed", "nut", "nuts", "berry", "berries",
        "turmeric", "ginger", "honey", "maple", "sweetener", "candy", "confection",
        "spice", "spices", "blend", "cocktail", "mocktail", "tonic",
        "elixir", "drops", "shot", "shots", "chug", "sip", "sipper",
        # from newswire/producthunt
        "meal kit", "zero sugar", "no alcohol", "adaptogenic", "functional drink",
        "cereal", "granola", "cuisine", "culinary", "recipe", "bar ",
    ],
    "Beauty": [
        "beauty", "cosmetic", "cosmetics", "skincare", "skin", "hair", "haircare",
        "nail", "glow", "radiant", "serum", "spa", "salon", "lash", "brow",
        "bloom", "lush", "glam", "glamour", "fragrance", "scent", "parfum",
        "lipstick", "lip", "blush", "bronze", "contour", "foundation", "toner",
        "moistur", "mask", "peel", "exfol", "cleanser", "micellar", "essence",
        "retinol", "peptide", "hyaluron", "sunscreen", "spf", "colour", "color",
        "pigment", "palette", "eyeshadow", "concealer", "highlight", "groom",
        "grooming", "shave", "razor", "wax", "scrub", "body", "lotion", "butter",
        "mist", "spritz", "deodorant", "dental", "teeth", "oral", "tanning",
        "bronzer", "self-tan", "curl", "wave", "straight", "dye", "tint",
        "balm", "gloss", "liner", "mascara", "blot", "prime", "primer",
        "aftershave", "cologne", "perfume", "powder", "setting", "finish",
        # from newswire/producthunt
        "skin care", "makeup", "hair care", "derma", "aesthetic",
    ],
    "Health/Wellness": [
        "health", "wellness", "vital", "vitality", "well", "heal", "healing",
        "longevity", "pure", "detox", "supplement", "supplements",
        "vitamin", "vitamins", "probiotic", "remedy", "relief", "therapeutic",
        "mindful", "mindfulness", "balance", "restore", "sleep", "immunity",
        "immune", "collagen", "omega", "hormone", "fertility", "menopause",
        "perimenopause", "period", "cycle", "menstrual", "postpartum",
        "medit", "meditation", "biohack", "longe", "lifespan", "microbiome",
        "nootropic", "ashwagandha", "functional", "integrative", "holistic",
        "ayurved", "herbal", "recover", "recovery", "therapy", "clinical",
        "weight", "metabol", "glp", "peptide", "iv", "infusion", "ozone",
        "cbd", "hemp", "melatonin", "magnesium", "zinc", "iron", "stress",
        "anxiety", "mood", "mental", "cognitive", "brain", "focus", "energy",
        # from newswire/producthunt
        "gut health", "mental health", "weight loss", "biohack",
    ],
    "Apparel": [
        "apparel", "clothing", "wear", "fashion", "style", "dress", "thread",
        "stitch", "cloth", "fabric", "couture", "gear", "denim", "outfitter",
        "outfitters", "wardrobe", "garment", "shoe", "shoes", "sneaker",
        "sneakers", "boot", "boots", "heel", "heels", "accessory", "accessories",
        "bag", "bags", "purse", "handbag", "wallet", "belt", "hat", "hats",
        "cap", "caps", "sock", "socks", "underwear", "lingerie", "intimates",
        "swimwear", "swim", "athleisure", "activewear", "sportswear",
        "streetwear", "luxury", "womenswear", "menswear", "kidswear",
        "jewelry", "jewellery", "ring", "rings", "necklace", "bracelet",
        "earring", "earrings", "pendant", "chain", "watch", "watches",
        "sunglasses", "eyewear", "glasses", "lens", "scarf", "scarves",
        "glove", "gloves", "hoodie", "sweatshirt", "tee", "tees", "tshirt",
        "pant", "pants", "short", "shorts", "skirt", "blazer", "jacket",
        "coat", "puffer", "down", "vest", "cardigan", "sweater", "knit",
        # from newswire/producthunt
        "shirt", "outerwear",
    ],
    "Fitness": [
        "fitness", "gym", "sport", "sports", "active", "athlete", "athletes",
        "train", "training", "run", "running", "lift", "lifting", "workout",
        "cycle", "cycling", "swim", "movement", "flex", "performance",
        "yoga", "pilates", "crossfit", "hiit", "cardio", "strength",
        "bodybuild", "endurance", "marathon", "triathlon", "rowing", "climb",
        "hike", "hiking", "camp", "camping", "surf", "paddle", "stretch",
        "recover", "foam", "roller", "mat", "band", "resistance", "weight",
        # from newswire/producthunt
        "exercise", "athletic", "strength training",
    ],
    "Home/Lifestyle": [
        "home", "house", "living", "decor", "design", "interior", "furnish",
        "furniture", "bed", "bath", "clean", "cleaning", "organize", "space",
        "nest", "den", "hearth", "habitat", "cookware", "cook", "bakeware",
        "knife", "knives", "cutlery", "plate", "plates", "bowl", "bowls",
        "mug", "mugs", "cup", "cups", "glass", "glasses", "candle", "candles",
        "diffuser", "aroma", "plant", "garden", "outdoor", "patio", "pillow",
        "pillows", "blanket", "throw", "rug", "rugs", "towel", "towels",
        "linen", "linens", "storage", "shelf", "frame", "lamp", "lighting",
        "pot", "pots", "pan", "pans", "wok", "cast iron", "ceramic",
        "bamboo", "sustainable", "reusable", "zero waste", "eco",
        # from newswire/producthunt
        "home decor", "organization", "pet food", "pet care", "baby",
        "parenting", "nursery", "pet accessory",
    ],
    "Consumer AI": [
        "ai", "intelligence", "intelligent", "smart", "digital", "lab", "labs",
        "tech", "app", "platform", "software", "data", "algorithm", "model",
        "neural", "machine", "automat", "personal", "assistant", "bot",
        # from newswire/producthunt
        "personalized", "ai-powered", "ai coach", "wearable",
        "smart device", "consumer app", "personalization",
    ],
    "Pet": [
        "pet", "pets", "dog", "dogs", "cat", "cats", "paw", "paws",
        "fur", "bark", "vet", "animal", "animals", "canine", "feline",
        "puppy", "kitten", "kibble", "treat", "treats", "leash", "collar",
        "poodle", "retriever", "breed", "rescue", "biscuit", "chew", "chews",
        "fetch", "wag", "tail", "snout", "pup", "hound", "terrier",
        # from producthunt
        "pet health", "veterinary", "dog treat", "cat treat",
        "dog food", "cat food",
    ],
    "Education": [
        "learn", "learning", "edu", "education", "school", "tutor", "coaching",
        "skill", "skills", "study", "teach", "academy", "knowledge",
        "course", "curriculum", "child", "children", "kid", "kids", "family",
        "parent", "parenting", "montessori", "stem", "craft",
    ],
    "Entertainment": [
        "entertain", "entertainment", "media", "content", "story", "stories",
        "game", "games", "gaming", "music", "art", "film", "studio",
        "creative", "experience", "streaming", "podcast", "creator", "play",
        "theater", "theatre", "concert", "live", "event", "ticket",
    ],
    "Finance": [
        "fintech", "payment", "payments", "money", "wallet", "credit", "wealth",
        "bank", "banking", "invest", "crypto", "defi", "neobank", "insurance",
        "saving", "savings", "borrow", "lending", "mortgage", "real estate",
        # from producthunt
        "budget", "personal finance",
    ],
    "Sports": [
        "sport", "sports", "ball", "team", "league", "athlete", "race",
        "compete", "competition", "soccer", "basketball", "football",
        "tennis", "golf", "hockey", "baseball", "esport", "fan", "fans",
    ],
}
