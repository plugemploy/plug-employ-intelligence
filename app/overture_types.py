"""Which Overture places count as each business type in app/catalog.py.

Each type lists Overture categories (its `taxonomy.primary` or `basic_category`) and, for types Overture has no
category for, words to look for in the business name. A type not listed here (one a customer typed) matches
categories and names containing its own words.
"""

import re

# business type -> (Overture categories, words in the business name)
TYPES: dict[str, tuple[list[str], list[str]]] = {
    # Health & Medical
    "Dentist": (["dental_clinic", "general_dentistry", "cosmetic_dentistry", "dentist"], ["dental", "dentist"]),
    "Orthodontist": (["orthodontics"], ["orthodont"]),
    "Pediatric dentist": (["pediatric_dentistry"], ["pediatric dent", "kids dent"]),
    "Oral surgeon": (["oral_and_maxillofacial_surgery"], ["oral surg"]),
    "Dermatology": (["dermatology"], ["dermatolog"]),
    "Plastic surgeon": (["plastic_and_reconstructive_surgery"], ["plastic surg"]),
    "Chiropractor": (["chiropractic"], ["chiropract"]),
    "Physical therapy": (["physical_therapy", "physical_medicine_and_rehabilitation"], ["physical therapy"]),
    "Optometrist": (["optometry", "vision_or_eye_care_clinic"], ["optometr", "eye care"]),
    "Pediatrician": (["pediatric_clinic"], ["pediatric"]),
    "Family doctor": (["family_practice", "primary_care_or_general_clinic", "doctors_office"], ["family medicine"]),
    "Urgent care": (["urgent_care_clinic", "emergency_or_urgent_care_facility"], ["urgent care"]),
    "Audiologist": (["audiology", "hearing_aid_store"], ["hearing", "audiolog"]),
    "Podiatrist": (["podiatry"], ["podiatr", "foot and ankle"]),
    "Mental health counselor": (["counseling", "behavioral_or_mental_health_clinic", "psychology", "family_counseling",
                                 "marriage_or_relationship_counseling"], ["counseling"]),
    "Weight loss clinic": (["weight_loss_center"], ["weight loss"]),
    "Pharmacy": (["pharmacy", "pharmacy_and_drug_store", "drugstore"], ["pharmacy"]),
    # Beauty & Wellness
    "Med spa": (["medical_spa"], ["med spa", "medspa", "aesthetics"]),
    "Day spa": (["day_spa", "spa", "health_spa"], ["day spa"]),
    "Salon": (["hair_salon", "beauty_salon", "kids_hair_salon"], ["salon"]),
    "Barber shop": (["barber"], ["barber"]),
    "Nail salon": (["nail_salon"], ["nail"]),
    "Massage therapist": (["massage_therapy"], ["massage"]),
    "Lash studio": (["eyelash_service", "eyebrow_service"], ["lash", "brow"]),
    "Waxing studio": (["waxing"], ["wax"]),
    "Tanning salon": (["tanning_salon", "spray_tanning"], ["tanning"]),
    "Tattoo shop": (["tattoo_and_piercing", "tattoo"], ["tattoo"]),
    "Fitness": (["gym", "fitness_studio", "sport_or_fitness_facility", "boot_camp", "boxing_gym"], ["fitness"]),
    "Yoga studio": (["yoga_studio"], ["yoga"]),
    "Pilates studio": (["pilates_studio"], ["pilates"]),
    "Personal trainer": (["fitness_trainer"], ["personal train"]),
    "Martial arts studio": (["martial_arts_club"], ["martial arts", "karate", "jiu jitsu", "taekwondo"]),
    "Dance studio": (["dance_studio"], ["dance"]),
    "CrossFit gym": ([], ["crossfit"]),
    # Home Services
    "HVAC": (["hvac_service"], ["heating", "air conditioning", "hvac"]),
    "Plumber": (["plumbing"], ["plumb"]),
    "Electrician": (["electrician"], ["electric"]),
    "Roofing": (["roofing"], ["roof"]),
    "Remodeling": (["altering_and_remodeling_contractor", "contractor"], ["remodel", "renovation"]),
    "Kitchen remodeling": (["kitchen_remodeling", "kitchen_and_bath_store"], ["kitchen"]),
    "Bathroom remodeling": (["bathroom_remodeling", "kitchen_and_bath_store"], ["bath"]),
    "Home builder": (["builder", "home_developer"], ["homes", "builders"]),
    "Pool": (["pool_and_hot_tub_service", "pool_cleaning", "hot_tub_and_pool_store"], ["pool"]),
    "Interior design": (["interior_design"], ["interior"]),
    "Pest control": (["pest_control_service"], ["pest", "termite"]),
    "Landscaping": (["landscaping", "landscape_architect", "gardener"], ["landscap"]),
    "Lawn care": (["lawn_service"], ["lawn"]),
    "Tree service": (["tree_service"], ["tree"]),
    "Painter": (["painting"], ["painting"]),
    "Flooring": (["flooring_contractor", "flooring_store", "carpet_store"], ["floor"]),
    "Cabinet maker": (["cabinet_sales_service"], ["cabinet"]),
    "Countertops": (["countertop_installation"], ["countertop", "granite"]),
    "Window and door company": (["windows_installation", "door_sales_service", "window_supplier"], ["window", "door"]),
    "Garage door company": (["garage_door_service"], ["garage door"]),
    "Fence company": (["fence_and_gate_sales_service"], ["fence"]),
    "Home cleaning service": (["home_cleaning", "cleaning_service"], ["cleaning", "maid"]),
    "Carpet cleaning": (["carpet_cleaning"], ["carpet clean"]),
    "Pressure washing": (["pressure_washing", "window_washing"], ["pressure wash", "power wash"]),
    "Solar installer": (["solar_installation"], ["solar"]),
    "Home security": (["security_systems", "home_security"], ["security"]),
    "Moving company": (["mover"], ["moving", "movers"]),
    "Junk removal": (["junk_removal_and_hauling"], ["junk", "hauling"]),
    "Handyman": (["handyman"], ["handyman"]),
    "Gutter company": (["gutter_service"], ["gutter"]),
    "Closet organizer": ([], ["closet", "organiz"]),
    # Food & Drink
    "Restaurant": (["restaurant"], []),
    "Fine dining restaurant": (["steakhouse", "seafood_restaurant", "italian_restaurant", "french_restaurant"], ["grill"]),
    "Cafe": (["cafe"], ["cafe"]),
    "Coffee shop": (["coffee_shop", "coffee_roastery"], ["coffee"]),
    "Bakery": (["bakery", "donut_shop", "dessert_shop"], ["bakery", "cake"]),
    "Brewery": (["brewery"], ["brew"]),
    "Winery": (["winery"], ["winery", "vineyard"]),
    "Wine shop": (["beer_wine_spirits_store", "liquor_store", "wine_bar"], ["wine"]),
    "Bar": (["bar", "cocktail_bar", "sports_bar", "pub", "beer_bar", "lounge"], ["tavern", "pub"]),
    "Caterer": (["caterer"], ["catering"]),
    "Food truck": (["food_truck_stand"], ["food truck"]),
    "Ice cream shop": (["ice_cream_shop"], ["ice cream", "creamery", "gelato"]),
    "Pizza restaurant": (["pizza_restaurant", "pizza_delivery_service"], ["pizza"]),
    "Butcher shop": (["butcher_shop"], ["butcher", "meat"]),
    # Retail & Shopping
    "Boutique": (["fashion_boutique", "womens_clothing_store", "fashion_accessories_store"], ["boutique"]),
    "Jeweler": (["jewelry_store"], ["jewel"]),
    "Furniture store": (["furniture_store"], ["furniture"]),
    "Home decor store": (["home_decor_store", "home_goods_store"], ["decor"]),
    "Gift shop": (["gift_shop", "flowers_and_gifts_store"], ["gift"]),
    "Florist": (["florist", "floral_designer", "flowers_and_gifts_store"], ["florist", "flower"]),
    "Bridal shop": (["bridal_shop"], ["bridal"]),
    "Shoe store": (["shoe_store"], ["shoe"]),
    "Children's clothing store": (["childrens_clothing_store"], ["kids", "children"]),
    "Bike shop": (["bike_store", "bike_repair_maintenance"], ["bike", "cycl"]),
    "Outdoor store": (["outdoor_store", "sporting_goods_store"], ["outdoor", "outfitter"]),
    "Mattress store": (["mattress_store"], ["mattress"]),
    "Appliance store": (["appliance_store"], ["appliance"]),
    "Art gallery": (["art_gallery"], ["gallery"]),
    "Bookstore": (["bookstore", "used_bookstore"], ["books"]),
    "Garden center": (["nursery_and_gardening_store"], ["garden", "nursery"]),
    "Optical store": (["eyewear_store"], ["optical", "eyewear"]),
    # Professional Services
    "Real estate": (["real_estate_agent", "real_estate_service"], ["realty", "real estate"]),
    "Mortgage lender": (["mortgage_lender", "mortgage_broker"], ["mortgage"]),
    "Financial advisor": (["financial_advising", "investing", "investment_management_company"], ["financial", "wealth"]),
    "Insurance agent": (["insurance_agency", "auto_insurance", "life_insurance", "home_and_rental_insurance"], ["insurance"]),
    "Law firm": (["attorney_or_law_firm", "legal_service", "personal_injury_law", "divorce_and_family_law",
                  "criminal_defense_law", "business_law"], ["law", "attorney"]),
    "Estate planning attorney": (["estate_planning_law", "wills_trusts_and_probate"], ["estate planning"]),
    "Accountant": (["accountant", "tax_service", "bookkeeper"], ["cpa", "accounting", "tax"]),
    "Bank": (["bank"], ["bank"]),
    "Credit union": (["credit_union"], ["credit union"]),
    "Title company": ([], ["title"]),
    "Property management": (["property_management"], ["property management"]),
    "Marketing agency": (["marketing_agency", "advertising_agency", "internet_marketing_service"], ["marketing"]),
    "Photographer": (["photography_service", "event_photography_service", "session_photography_service"], ["photograph"]),
    "Printing service": (["printing_service", "commercial_printer", "screen_printing_service", "t_shirt_printing_service",
                          "sign_making"], ["print"]),
    "Senior living": (["retirement_home", "assisted_living_facility", "senior_living_facility"], ["senior living"]),
    "Home care agency": (["home_health_care"], ["home care", "in home"]),
    # Family & Education
    "Private school": (["private_school", "religious_school"], ["academy"]),
    "Preschool": (["preschool", "day_care_preschool"], ["preschool"]),
    "Daycare": (["child_care_and_day_care", "day_care_preschool"], ["daycare", "child care"]),
    "Tutoring": (["tutoring_service", "private_tutor"], ["tutor", "learning"]),
    "Kids activities": (["kids_recreation_and_party", "gymnastics_center"], ["kids"]),
    "Music lessons": (["music_school"], ["music"]),
    "Swim school": (["swimming_instructor"], ["swim"]),
    "Summer camp": (["educational_camp"], ["camp"]),
    "College prep": ([], ["college prep", "test prep"]),
    "Driving school": (["driving_school"], ["driving school"]),
    # Pets
    "Veterinarian": (["veterinarian", "veterinary_care", "emergency_pet_hospital"], ["veterinar", "animal hospital"]),
    "Pet grooming": (["pet_groomer"], ["pet groom", "dog groom", "dogwash", "dog wash"]),
    "Dog trainer": (["dog_trainer"], ["dog train"]),
    "Pet boarding": (["pet_boarding", "pet_sitting"], ["boarding", "kennel"]),
    "Pet store": (["pet_store", "animal_and_pet_store"], ["pet"]),
    # Auto
    "Auto repair": (["automotive_repair", "automotive_service", "oil_change_station"], ["auto repair", "automotive"]),
    "Car dealership": (["auto_dealer", "used_auto_dealer"], ["motors", "auto sales"]),
    "Auto detailing": (["auto_detailing"], ["detail"]),
    "Tire shop": (["tire_shop", "tire_dealer_and_repair"], ["tire"]),
    "Car wash": (["car_wash"], ["car wash"]),
    "Body shop": (["auto_body_shop"], ["collision", "body shop"]),
    # Events & Lifestyle
    "Event venue": (["event_venue"], ["venue", "event center"]),
    "Wedding venue": (["event_venue", "wedding_chapel"], ["wedding", "venue"]),
    "Event planner": (["party_and_event_planning"], ["events"]),
    "Wedding planner": (["wedding_planning"], ["wedding"]),
    "Golf": (["golf_course", "golf_club", "golf_instructor", "driving_range"], ["golf"]),
    "Country club": (["country_club"], ["country club"]),
    "Travel agency": (["travel_agent", "travel_service", "travel_company"], ["travel"]),
    "Hotel": (["hotel"], ["hotel", "inn"]),
    "Boat dealer": (["boat_dealer"], ["marine", "boat"]),
    "Entertainment venue": (["arts_and_entertainment", "music_venue", "performing_arts_venue", "amusement_park"], []),
    "Escape room": (["escape_room"], ["escape"]),
    "Bowling alley": (["bowling_alley"], ["bowl"]),
}

# Overture categories too general to say what a business does: a name word may decide these.
GENERIC = {"contractor", "home_service", "professional_service", "shopping", "health_care", "b2b_service",
           "building_or_construction_service", "personal_or_beauty_service", "wellness_service", "medical_service",
           "specialty_store", "retail", "sports_and_recreation", "sport_or_fitness_facility", "education",
           "educational_service", "event_or_party_service", "arts_and_entertainment", "financial_service",
           "real_estate_service", "automotive_service", "family_service", "design_service", "media_service"}

# Types whose name words are too common to search on their own: only their categories count.
CATEGORY_ONLY = {"Restaurant", "Entertainment venue", "Fitness", "Remodeling", "Bar", "Bank", "Hotel"}


def name_pattern(words: list[str]) -> str:
    """One regex for a list of name words: short words must stand alone (plural ok), longer ones start a word."""
    parts = [rf"\b{re.escape(w)}(s|es)?\b" if len(w) <= 4 else rf"\b{re.escape(w)}" for w in words]
    return "(" + "|".join(parts) + ")" if parts else ""


def _words(name: str) -> list[str]:
    return [w for w in re.split(r"[^a-z0-9]+", name.lower()) if len(w) > 2]


def matcher(business_type: str) -> tuple[list[str], list[str]]:
    """(categories, name words) for a business type. A type someone typed matches on its own words."""
    if business_type in TYPES:
        cats, words = TYPES[business_type]
        return cats, ([] if business_type in CATEGORY_ONLY else words)
    words = _words(business_type)
    return ["_".join(words)] if words else [], [" ".join(words)] if words else []
