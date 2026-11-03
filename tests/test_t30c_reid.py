"""T30c second-pass false-warning guards: new examples written for this change.

Every person, place and value below is invented for the test.
"""

from __future__ import annotations

from shadowfenced import reid


def _kinds(text: str) -> set[str]:
    return {item["kind"] for item in reid.extract_quasi_identifiers(text)}


# 1. Pronouns alone are not gender -------------------------------------------
def test_pronoun_only_gender_does_not_warn():
    result = reid.assess("She is 34 years old and lives in Dunmere.")
    assert result.outcome == "OK"
    assert "gender" not in result.combination


def test_two_pronouns_with_town_and_age_do_not_warn():
    result = reid.assess("I sent him to Dunmere; he is 45.")
    assert result.outcome == "OK"
    assert "gender" not in result.combination


def test_explicit_female_still_counts():
    result = reid.assess("Born 14 March 1989, postcode 90274, female.")
    assert result.outcome == "HOLD"
    assert "gender" in result.combination


def test_explicit_man_statement_counts_as_gender():
    assert "gender" in _kinds("A 34-year-old man moved to Dunmere.")


def test_gender_declaration_counts_as_gender():
    result = reid.assess("Gender: M, born 14 March 1989, postcode 90274.")
    assert result.outcome == "HOLD"
    assert "gender" in result.combination


# 2. A lone first name is not a name ------------------------------------------
def test_verb_led_first_name_is_not_a_name():
    result = reid.assess("Tell Jake I said hi.")
    assert result.outcome == "OK"
    assert "name" not in result.combination


def test_ask_led_first_name_is_not_a_name():
    assert reid.assess("Ask Priya to call back tomorrow.").outcome == "OK"


def test_real_full_name_still_holds():
    assert reid.assess("Mara Vell, born 14 March 1989.").outcome == "HOLD"


# 3. Public-figure markers ----------------------------------------------------
def test_pop_star_birthday_does_not_warn():
    text = "Pop star Wren Kado was born on 3 April 1985."
    assert reid.assess(text).outcome == "OK"


def test_parenthesised_public_figure_birthday_does_not_warn():
    text = "(Public figure) Wren Kado was born on 3 April 1985."
    assert reid.assess(text).outcome == "OK"


def test_bands_singer_birthday_does_not_warn():
    text = "The band's singer Wren Kado was born on 3 April 1985."
    assert reid.assess(text).outcome == "OK"


def test_bare_senator_birthday_does_not_warn():
    text = "Senator Mara Vell was born on 3 April 1985."
    assert reid.assess(text).outcome == "OK"


def test_bare_singer_birthday_does_not_warn():
    text = "Singer Wren Kado was born on 3 April 1985."
    assert reid.assess(text).outcome == "OK"


def test_press_coverage_marks_public_figure_across_turns():
    prior = ["She is all over the press these days."]
    result = reid.assess(
        "Wren Kado was born on 3 April 1985.", conversation=prior)
    assert result.outcome == "OK"


def test_private_birthday_without_markers_still_holds():
    assert reid.assess("Wren Kado was born on 3 April 1985.").outcome == "HOLD"


# 4. Businesses ----------------------------------------------------------------
def test_shop_address_phone_and_hours_are_business_details():
    text = ("Visit our shop at 12 Mill Lane, Dunmere, open Mon-Sat 9-5, "
            "for bookings call 555-0142.")
    assert reid.assess(text).outcome == "OK"


def test_cafe_email_for_bookings_is_a_business_detail():
    text = ("Our cafe on Salt Row opens late; email cafe@saltrowcafe.com "
            "for bookings.")
    assert reid.assess(text).outcome == "OK"


def test_trade_enquiries_mark_phone_as_business():
    text = "Trade enquiries: call 555-0142 in Dunmere."
    assert reid.assess(text).outcome == "OK"


def test_personal_address_still_holds():
    assert reid.assess("Mara Vell of 9 Bellflower Lane.").outcome == "HOLD"


# 5. Job adverts -----------------------------------------------------------------
def test_hiring_advert_with_role_mailbox_is_not_personal():
    text = "We are hiring a nurse in Dunmere; apply to jobs@dunmereclinic.com."
    assert reid.assess(text).outcome == "OK"


def test_vacancy_with_experience_level_is_not_personal():
    text = ("Vacancy: night-shift technician in Gullhaven, experience "
            "level 2+. Contact hr@gullhavenworks.com.")
    assert reid.assess(text).outcome == "OK"


def test_tours_mailbox_marks_job_title_and_town_as_role():
    text = "We need a driver in Dunmere; contact tours@dunmeretours.com."
    assert reid.assess(text).outcome == "OK"


# 6. Aggregate statistics ----------------------------------------------------------
def test_patient_counts_bands_and_percentages_are_group_data():
    text = "42 patients aged 30\u201339 took part; 60% were female."
    result = reid.assess(text)
    assert result.outcome == "OK"
    assert "gender" not in result.combination
    assert "age" not in result.combination


def test_patient_count_with_pronoun_and_age_is_group_data():
    text = "42 patients visited the Dunmere clinic. She is 34 years old."
    assert reid.assess(text).outcome == "OK"


def test_average_and_area_code_are_group_data():
    text = "In area code 90274, the average age is 45."
    assert reid.assess(text).outcome == "OK"


# Singleton roles still identify -----------------------------------------------------
def test_singleton_role_plus_town_still_warns():
    text = "The only night-shift dispatcher at Greyhook Point, page him."
    assert reid.assess(text).outcome == "WARN"
