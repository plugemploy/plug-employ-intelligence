"""Business types customers can search for, grouped for the industry picker.

Each name is used as plain text in the Google search ("Florist in 37934"), so customers can also type their own.
"""

CATEGORIES: dict[str, list[str]] = {
    "Health & Medical": [
        "Dentist", "Orthodontist", "Pediatric dentist", "Oral surgeon", "Dermatology", "Plastic surgeon",
        "Chiropractor", "Physical therapy", "Optometrist", "Pediatrician", "Family doctor", "Urgent care",
        "Audiologist", "Podiatrist", "Mental health counselor", "Weight loss clinic", "Pharmacy",
    ],
    "Beauty & Wellness": [
        "Med spa", "Day spa", "Salon", "Barber shop", "Nail salon", "Massage therapist", "Lash studio",
        "Waxing studio", "Tanning salon", "Tattoo shop", "Fitness", "Yoga studio", "Pilates studio",
        "Personal trainer", "Martial arts studio", "Dance studio", "CrossFit gym",
    ],
    "Home Services": [
        "HVAC", "Plumber", "Electrician", "Roofing", "Remodeling", "Kitchen remodeling", "Bathroom remodeling",
        "Home builder", "Pool", "Interior design", "Pest control", "Landscaping", "Lawn care", "Tree service",
        "Painter", "Flooring", "Cabinet maker", "Countertops", "Window and door company", "Garage door company",
        "Fence company", "Home cleaning service", "Carpet cleaning", "Pressure washing", "Solar installer",
        "Home security", "Moving company", "Junk removal", "Handyman", "Gutter company", "Closet organizer",
    ],
    "Food & Drink": [
        "Restaurant", "Fine dining restaurant", "Cafe", "Coffee shop", "Bakery", "Brewery", "Winery",
        "Wine shop", "Bar", "Caterer", "Food truck", "Ice cream shop", "Pizza restaurant", "Butcher shop",
    ],
    "Retail & Shopping": [
        "Boutique", "Jeweler", "Furniture store", "Home decor store", "Gift shop", "Florist", "Bridal shop",
        "Shoe store", "Children's clothing store", "Bike shop", "Outdoor store", "Mattress store",
        "Appliance store", "Art gallery", "Bookstore", "Garden center", "Optical store",
    ],
    "Professional Services": [
        "Real estate", "Mortgage lender", "Financial advisor", "Insurance agent", "Law firm", "Estate planning attorney",
        "Accountant", "Bank", "Credit union", "Title company", "Property management", "Marketing agency",
        "Photographer", "Printing service", "Senior living", "Home care agency",
    ],
    "Family & Education": [
        "Private school", "Preschool", "Daycare", "Tutoring", "Kids activities", "Music lessons",
        "Swim school", "Summer camp", "College prep", "Driving school",
    ],
    "Pets": ["Veterinarian", "Pet grooming", "Dog trainer", "Pet boarding", "Pet store"],
    "Auto": ["Auto repair", "Car dealership", "Auto detailing", "Tire shop", "Car wash", "Body shop"],
    "Events & Lifestyle": [
        "Event venue", "Wedding venue", "Event planner", "Wedding planner", "Golf", "Country club",
        "Travel agency", "Hotel", "Boat dealer", "Entertainment venue", "Escape room", "Bowling alley",
    ],
}

ALL: list[str] = [name for names in CATEGORIES.values() for name in names]
