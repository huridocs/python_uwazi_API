#!/usr/bin/env python3
"""Generate 5 example pages combining different blocks and vibes."""

from pathlib import Path

from uwazi_agent.drivers.page_builder.renderer import PageRenderer

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"


def example_1_corporate_annual_report() -> None:
    """Corporate vibe: Annual impact report with stats and timeline."""
    renderer = PageRenderer(BASE_DIR)
    html = renderer.render(
        vibe="corporate",
        blocks=[
            {
                "type": "hero",
                "slots": {
                    "category_label": "Annual Report 2025",
                    "title": "Documenting impact across borders and decades.",
                    "subtitle": "Our 2025 report on advancing human rights documentation, evidence preservation, and international advocacy.",
                    "height": "medium",
                },
            },
            {
                "type": "stats_grid",
                "slots": {
                    "heading": "By the numbers",
                    "columns": 4,
                    "stats": [
                        {"value": "12,847", "label": "Documents Processed"},
                        {"value": "3,200+", "label": "Partner Organizations"},
                        {"value": "47", "label": "Countries Reached"},
                        {"value": "98.7%", "label": "Data Accuracy"},
                    ],
                },
            },
            {
                "type": "two_column",
                "slots": {
                    "heading": "Key achievements",
                    "left_width": "60%",
                    "left_blocks": [
                        {
                            "type": "content",
                            "slots": {
                                "heading": "Documentation and evidence",
                                "max_width": "wide",
                                "body_html": (
                                    "<p>This year, our network of field researchers and legal experts "
                                    "documented 12,847 cases of human rights violations across 47 countries. "
                                    "The evidence collected has been instrumental in 213 legal proceedings "
                                    "before national and international courts.</p>"
                                    "<p>We expanded our digital forensics capabilities, introducing AI-assisted "
                                    "chain-of-custody verification that reduced documentation errors by 76% "
                                    "compared to manual processes.</p>"
                                    "<h3>Notable victories</h3>"
                                    "<ul>"
                                    "<li>Successfully supported prosecution of 43 cases of forced displacement</li>"
                                    "<li>Provided critical evidence in 28 cases before the Inter-American Court</li>"
                                    "<li>Launched 6 new country programs in Sub-Saharan Africa</li>"
                                    "</ul>"
                                ),
                            },
                        }
                    ],
                    "right_blocks": [
                        {
                            "type": "card_grid",
                            "slots": {
                                "heading": "Regional highlights",
                                "columns": 1,
                                "cards": [
                                    {
                                        "title": "Latin America",
                                        "description": "2,300+ cases documented; 15 legal victories in environmental rights cases.",
                                    },
                                    {
                                        "title": "Southeast Asia",
                                        "description": "New partnership programs in Myanmar, Cambodia, and the Philippines.",
                                    },
                                    {
                                        "title": "Sub-Saharan Africa",
                                        "description": "6 new country programs launched with 45 local partner organizations.",
                                    },
                                ],
                            },
                        }
                    ],
                },
            },
            {
                "type": "divider",
                "slots": {},
            },
            {
                "type": "event_chart_horizontal",
                "slots": {
                    "title": "Release timeline",
                    "show_descriptions": "false",
                    "events": [
                        {"date": "Q1 2024", "title": "Alpha", "description": "Initial release with core features"},
                        {"date": "Q2 2024", "title": "Beta", "description": "Public beta with feedback loop"},
                        {"date": "Q3 2024", "title": "v1.0", "description": "First stable release"},
                        {"date": "Q4 2024", "title": "v1.1", "description": "Performance and stability"},
                        {"date": "Q1 2025", "title": "v2.0", "description": "Major new features"},
                    ],
                },
            },
            {
                "type": "bar_chart",
                "slots": {
                    "title": "Cases by violation type",
                    "data": [
                        {"label": "Forced displacement", "value": 3420},
                        {"label": "Arbitrary detention", "value": 2890},
                        {"label": "Extrajudicial killings", "value": 1567},
                        {"label": "Torture", "value": 1240},
                        {"label": "Forced disappearances", "value": 890},
                        {"label": "Sexual violence", "value": 620},
                    ],
                },
            },
            {
                "type": "timeline",
                "slots": {
                    "heading": "2025 milestones",
                    "entries": [
                        {
                            "date": "January",
                            "title": "Uwazi Platform v4.0 launch",
                            "description": "Released major platform update with real-time collaboration and enhanced evidence management capabilities.",
                        },
                        {
                            "date": "March",
                            "title": "African Union partnership",
                            "description": "Signed MOU with the African Commission on Human and Peoples' Rights for joint documentation initiatives.",
                        },
                        {
                            "date": "July",
                            "title": "AI-assisted document analysis",
                            "description": "Deployed machine learning models for automated entity extraction and cross-referencing in 12 languages.",
                        },
                        {
                            "date": "October",
                            "title": "Global investigative journalism network",
                            "description": "Launched collaborative platform connecting 200+ investigative journalists with our evidence database.",
                        },
                    ],
                },
            },
            {
                "type": "cta",
                "slots": {
                    "category_label": "Get involved",
                    "title": "Join us in documenting justice",
                    "subtitle": "Become a partner organization and access our full suite of documentation tools.",
                    "button_text": "Partner with us",
                    "button_link": "/contact/partnerships",
                },
            },
        ],
    )
    out = OUTPUT_DIR / "example_1_corporate_annual_report.html"
    out.write_text(html, encoding="utf-8")
    print(f"  [OK] {out.name}")


def example_2_activist_urgent_action() -> None:
    """Activist vibe: Urgent human rights alert page."""
    renderer = PageRenderer(BASE_DIR)
    html = renderer.render(
        vibe="activist",
        blocks=[
            {
                "type": "hero",
                "slots": {
                    "category_label": "Urgent Action",
                    "title": "Mass detentions in the Coastal Region.",
                    "subtitle": "Over 200 peaceful protesters detained since June 1st. Legal observers denied access. Immediate international action required.",
                    "height": "medium",
                },
            },
            {
                "type": "stats_grid",
                "slots": {
                    "heading": "Situation report",
                    "columns": 3,
                    "stats": [
                        {"value": "213", "label": "Confirmed Detentions"},
                        {"value": "45", "label": "At Risk of Torture"},
                        {"value": "0", "label": "Legal Access Granted"},
                    ],
                },
            },
            {
                "type": "content",
                "slots": {
                    "heading": "What is happening",
                    "body_html": (
                        "<p>On June 1st, 2026, security forces began a coordinated crackdown on peaceful "
                        "environmental protests in the Coastal Region. Demonstrators were calling for "
                        "transparency in offshore drilling permits affecting indigenous territories.</p>"
                        "<p>Eyewitness accounts and satellite imagery confirm that at least 213 individuals "
                        "have been detained in undisclosed locations. Families report that detainees are "
                        "being held incommunicado, with no access to legal representation or medical care.</p>"
                        "<h3>Confirmed violations</h3>"
                        "<ul>"
                        "<li>Arbitrary arrest and detention without warrant</li>"
                        "<li>Denial of habeas corpus in 100% of documented cases</li>"
                        "<li>Use of excessive force against peaceful assembly</li>"
                        "<li>Targeted detention of human rights defenders and journalists</li>"
                        "</ul>"
                    ),
                },
            },
            {
                "type": "card_grid",
                "slots": {
                    "heading": "Take action now",
                    "columns": 3,
                    "cards": [
                        {
                            "title": "Spread awareness",
                            "description": "Share verified information on social media using #CoastalRegionCrisis. Every share reaches decision makers.",
                            "link": "/resources/social-media-kit",
                        },
                        {
                            "title": "Write to authorities",
                            "description": "Use our template to send urgent appeals to the Ministry of Interior and international bodies.",
                            "link": "/action/write-letter",
                        },
                        {
                            "title": "Donate to legal fund",
                            "description": "Support emergency legal aid for detainees and their families. 100% goes to legal representation.",
                            "link": "/donate/legal-fund",
                        },
                    ],
                },
            },
            {
                "type": "cta",
                "slots": {
                    "category_label": "Act now",
                    "title": "Time is running out",
                    "subtitle": "The first 48 hours are critical. Your voice can make the difference between justice and impunity.",
                    "button_text": "Send urgent appeal",
                    "button_link": "/action/urgent-appeal",
                },
            },
        ],
    )
    out = OUTPUT_DIR / "example_2_activist_urgent_action.html"
    out.write_text(html, encoding="utf-8")
    print(f"  [OK] {out.name}")


def example_3_earth_environmental_data() -> None:
    """Earth vibe: Environmental monitoring dashboard."""
    renderer = PageRenderer(BASE_DIR)
    html = renderer.render(
        vibe="earth",
        blocks=[
            {
                "type": "hero",
                "slots": {
                    "category_label": "Live Monitor",
                    "title": "Amazon basin deforestation at a glance.",
                    "subtitle": "Real-time data from satellite imagery, field reports, and indigenous community monitoring networks.",
                    "height": "medium",
                },
            },
            {
                "type": "stats_grid",
                "slots": {
                    "heading": "Current status",
                    "columns": 4,
                    "stats": [
                        {"value": "11,568", "label": "km² Lost This Year"},
                        {"value": "12.3%", "label": "Year-over-Year Decrease"},
                        {"value": "3,847", "label": "Active Fire Hotspots"},
                        {"value": "42", "label": "Protected Areas at Risk"},
                    ],
                },
            },
            {
                "type": "two_column",
                "slots": {
                    "heading": "Deforestation by driver",
                    "left_width": "55%",
                    "left_blocks": [
                        {
                            "type": "bar_chart",
                            "slots": {
                                "title": "Primary causes of forest loss",
                                "unit": "%",
                                "data": [
                                    {"label": "Cattle ranching", "value": 38},
                                    {"label": "Soy cultivation", "value": 24},
                                    {"label": "Illegal mining", "value": 16},
                                    {"label": "Logging", "value": 12},
                                    {"label": "Other", "value": 10},
                                ],
                            },
                        }
                    ],
                    "right_blocks": [
                        {
                            "type": "pie_chart",
                            "slots": {
                                "title": "Geographic distribution",
                                "chart_type": "donut",
                                "center_label": "Total",
                                "data": [
                                    {"label": "Brazil", "value": 48},
                                    {"label": "Peru", "value": 22},
                                    {"label": "Bolivia", "value": 14},
                                    {"label": "Colombia", "value": 10},
                                    {"label": "Ecuador", "value": 6},
                                ],
                            },
                        }
                    ],
                },
            },
            {
                "type": "divider",
                "slots": {},
            },
            {
                "type": "event_chart_vertical",
                "slots": {
                    "title": "Monitoring history",
                    "events": [
                        {
                            "date": "January 2024",
                            "title": "First monitoring station deployed",
                            "description": "Initial 3 stations installed in the Kaxinawa territory, covering 12,000 hectares.",
                        },
                        {
                            "date": "March 2024",
                            "title": "Satellite imagery integration",
                            "description": "Connected to daily MODIS and Sentinel-2 feeds for change detection.",
                        },
                        {
                            "date": "June 2024",
                            "title": "Field network expansion",
                            "description": "Added 8 community monitors across 4 provinces; total 24 stations.",
                        },
                        {
                            "date": "September 2024",
                            "title": "First illegal mining detection",
                            "description": "Identified 2.3 km² of new mining activity within 48 hours of satellite alert.",
                        },
                        {
                            "date": "December 2024",
                            "title": "Annual report published",
                            "description": "First public report; 847 field reports, 3,200 hectares of deforestation verified.",
                        },
                        {
                            "date": "March 2025",
                            "title": "Drone fleet addition",
                            "description": "12 autonomous drones added for high-resolution verification of satellite alerts.",
                        },
                        {
                            "date": "June 2025",
                            "title": "Cross-border data sharing",
                            "description": "MOU signed with Peruvian and Colombian monitoring networks for joint operations.",
                        },
                        {
                            "date": "September 2025",
                            "title": "AI-assisted change detection",
                            "description": "ML models deployed; 92% reduction in false-positive alerts.",
                        },
                        {
                            "date": "January 2026",
                            "title": "Real-time public dashboard",
                            "description": "Open data portal launched with live deforestation metrics.",
                        },
                    ],
                },
            },
            {
                "type": "timeline",
                "slots": {
                    "heading": "Recent field reports",
                    "entries": [
                        {
                            "date": "June 10",
                            "title": "New illegal road detected in indigenous territory",
                            "description": "Satellite imagery reveals a 45 km unauthorized road penetrating the Kaxinawa indigenous reserve.",
                        },
                        {
                            "date": "June 5",
                            "title": "Gold mining surge in Peruvian Amazon",
                            "description": "Dredging operations detected along 32 km of the Madre de Dios river. Mercury contamination levels exceed WHO safety limits by 14x.",
                        },
                        {
                            "date": "May 28",
                            "title": "Brazilian enforcement operation success",
                            "description": "IBAMA agents dismantled 12 illegal logging camps in the Jamanxim National Forest. 4,500 m³ of illegally harvested timber seized.",
                        },
                    ],
                },
            },
            {
                "type": "cta",
                "slots": {
                    "category_label": "Support",
                    "title": "Fund indigenous-led monitoring",
                    "subtitle": "Your contribution funds satellite data access, field equipment, and legal advocacy for forest guardians.",
                    "button_text": "Donate now",
                    "button_link": "/donate/forest-guardians",
                },
            },
        ],
    )
    out = OUTPUT_DIR / "example_3_earth_environmental_data.html"
    out.write_text(html, encoding="utf-8")
    print(f"  [OK] {out.name}")


def example_4_minimal_academic_research() -> None:
    """Minimal vibe: Academic research archive landing page."""
    renderer = PageRenderer(BASE_DIR)
    html = renderer.render(
        vibe="minimal",
        blocks=[
            {
                "type": "hero",
                "slots": {
                    "category_label": "Digital Archive",
                    "title": "Transitional justice records, 1975 to 2025.",
                    "subtitle": "A comprehensive repository of truth commission records, witness testimonies, and legal proceedings.",
                    "height": "small",
                },
            },
            {
                "type": "content",
                "slots": {
                    "heading": "About the archive",
                    "body_html": (
                        "<p>This archive brings together dispersed records from 38 truth commissions, "
                        "12 international tribunals, and 56 national reconciliation processes. It serves "
                        "as a primary source repository for researchers, legal practitioners, and policymakers "
                        "working in the field of transitional justice.</p>"
                        "<p>The collection spans five decades and includes digitized witness statements, "
                        "commission reports, forensic evidence records, amnesty hearing transcripts, and "
                        "reparations program documentation.</p>"
                    ),
                },
            },
            {
                "type": "divider",
                "slots": {},
            },
            {
                "type": "card_grid",
                "slots": {
                    "heading": "Collections",
                    "columns": 3,
                    "cards": [
                        {
                            "title": "Truth Commissions",
                            "description": "Full records from 38 truth and reconciliation commissions, including the South African TRC, Peruvian CVR, and Sierra Leone TRC.",
                            "link": "/collections/truth-commissions",
                        },
                        {
                            "title": "International Tribunals",
                            "description": "Court records, indictments, and judgments from the ICTY, ICTR, ICC, and hybrid tribunals in Cambodia and Sierra Leone.",
                            "link": "/collections/tribunals",
                        },
                        {
                            "title": "Witness Testimonies",
                            "description": "42,000+ digitized oral testimonies from survivors, perpetrators, and witnesses across 56 conflict contexts.",
                            "link": "/collections/testimonies",
                        },
                        {
                            "title": "Forensic Evidence",
                            "description": "Forensic anthropology reports, DNA analysis records, and mass grave documentation from 18 countries.",
                            "link": "/collections/forensics",
                        },
                        {
                            "title": "Reparations Programs",
                            "description": "Documentation of 23 national reparations programs, including beneficiary registries and program evaluations.",
                            "link": "/collections/reparations",
                        },
                        {
                            "title": "Academic Publications",
                            "description": "Curated bibliography of 8,300+ peer-reviewed articles and monographs citing archival materials from this collection.",
                            "link": "/collections/publications",
                        },
                    ],
                },
            },
            {
                "type": "divider",
                "slots": {},
            },
            {
                "type": "stats_grid",
                "slots": {
                    "heading": "Archive by the numbers",
                    "columns": 4,
                    "stats": [
                        {"value": "38", "label": "Truth Commissions"},
                        {"value": "56", "label": "Countries Represented"},
                        {"value": "42K+", "label": "Witness Testimonies"},
                        {"value": "8.3K+", "label": "Academic Citations"},
                    ],
                },
            },
            {
                "type": "bar_chart",
                "slots": {
                    "title": "Records by source type",
                    "data": [
                        {"label": "Commission reports", "value": 12400},
                        {"label": "Witness statements", "value": 42000},
                        {"label": "Court records", "value": 18700},
                        {"label": "Forensic reports", "value": 3200},
                        {"label": "Reparations docs", "value": 5600},
                    ],
                },
            },
            {
                "type": "cta",
                "slots": {
                    "category_label": "Access",
                    "title": "Request research access",
                    "subtitle": "Access to sensitive materials requires institutional affiliation and research ethics approval.",
                    "button_text": "Apply for access",
                    "button_link": "/access/apply",
                },
            },
        ],
    )
    out = OUTPUT_DIR / "example_4_minimal_academic_research.html"
    out.write_text(html, encoding="utf-8")
    print(f"  [OK] {out.name}")


def example_5_warm_community_education() -> None:
    """Warm vibe: Community education and well-being program page."""
    renderer = PageRenderer(BASE_DIR)
    html = renderer.render(
        vibe="warm",
        blocks=[
            {
                "type": "hero",
                "slots": {
                    "category_label": "Our Mission",
                    "title": "Building resilient communities from within.",
                    "subtitle": "Empowering local leaders with the tools, knowledge, and networks to create lasting change.",
                    "height": "medium",
                },
            },
            {
                "type": "card_grid",
                "slots": {
                    "heading": "Our programs",
                    "columns": 3,
                    "cards": [
                        {
                            "title": "Community Education",
                            "description": "Participatory workshops on rights awareness, civic engagement, and digital literacy for 15,000+ participants annually.",
                            "link": "/programs/education",
                        },
                        {
                            "title": "Mental Health Support",
                            "description": "Trauma-informed counseling and peer support networks for communities affected by conflict and displacement.",
                            "link": "/programs/mental-health",
                        },
                        {
                            "title": "Livelihood Development",
                            "description": "Skills training, micro-grant programs, and cooperative formation support for sustainable community enterprise.",
                            "link": "/programs/livelihoods",
                        },
                    ],
                },
            },
            {
                "type": "two_column",
                "slots": {
                    "heading": "Spotlight: Women's Cooperative Network",
                    "left_width": "45%",
                    "left_blocks": [
                        {
                            "type": "content",
                            "slots": {
                                "body_html": (
                                    "<p>The Women's Cooperative Network launched in 2023 with 12 founding members "
                                    "in the Eastern Province. Today, it encompasses 47 cooperatives across 5 provinces, "
                                    "directly benefiting over 2,800 women and their families.</p>"
                                    "<p>Through shared resources, collective bargaining, and peer mentorship, "
                                    "cooperative members have increased their average household income by 340% "
                                    "while building lasting community infrastructure.</p>"
                                ),
                            },
                        }
                    ],
                    "right_blocks": [
                        {
                            "type": "stats_grid",
                            "slots": {
                                "columns": 2,
                                "stats": [
                                    {"value": "2,800+", "label": "Women Members"},
                                    {"value": "47", "label": "Cooperatives"},
                                    {"value": "340%", "label": "Income Growth"},
                                    {"value": "5", "label": "Provinces"},
                                ],
                            },
                        }
                    ],
                },
            },
            {
                "type": "divider",
                "slots": {},
            },
            {
                "type": "timeline",
                "slots": {
                    "heading": "Community voices",
                    "entries": [
                        {
                            "date": "March 2026",
                            "title": "For the first time, my children have a future.",
                            "description": "After joining the Mpanga Cooperative, Amina secured a microloan to start a tailoring business. Her three children are now in school full-time.",
                        },
                        {
                            "date": "February 2026",
                            "title": "Cooperative opens community health center",
                            "description": "The Rukwa Valley Cooperative pooled resources to build a health clinic, serving 12 villages that previously had no medical access within 50 km.",
                        },
                        {
                            "date": "December 2025",
                            "title": "Collective bargaining victory",
                            "description": "Members of the Kitale Textile Cooperative successfully negotiated a 60% price increase from wholesale buyers through unified negotiation.",
                        },
                    ],
                },
            },
            {
                "type": "cta",
                "slots": {
                    "category_label": "Donate",
                    "title": "Support a cooperative",
                    "subtitle": "Your donation provides seed funding, training materials, and mentorship for a new women's cooperative.",
                    "button_text": "Make a difference",
                    "button_link": "/donate/cooperatives",
                },
            },
        ],
    )
    out = OUTPUT_DIR / "example_5_warm_community_education.html"
    out.write_text(html, encoding="utf-8")
    print(f"  [OK] {out.name}")


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print("Generating example pages...\n")
    example_1_corporate_annual_report()
    example_2_activist_urgent_action()
    example_3_earth_environmental_data()
    example_4_minimal_academic_research()
    example_5_warm_community_education()
    print(
        f"\nDone! {len(list(OUTPUT_DIR.glob('*.html')))} pages written to {OUTPUT_DIR.relative_to(BASE_DIR.parent.parent)}"
    )


if __name__ == "__main__":
    main()
