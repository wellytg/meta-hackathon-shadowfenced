"""T30b false-warning guards: new examples written for this change.

Every person, place and value below is invented for the test.
"""

from __future__ import annotations

from shadowfenced import reid


def test_single_dob_alone_never_warns():
    assert reid.assess("Born 14 March 1989.").outcome == "OK"


def test_birth_year_alone_never_counts_as_dob():
    result = reid.assess("Born in 1991.")
    assert result.outcome == "OK"
    assert "dob" not in result.combination


def test_age_alone_never_counts_as_dob():
    result = reid.assess("She is 34 years old.")
    assert "dob" not in result.combination
    assert result.outcome == "OK"


def test_lone_first_name_alone_is_ok():
    assert reid.assess("Liora called earlier today.").outcome == "OK"


def test_public_role_birthday_does_not_warn():
    result = reid.assess("The actor Julian Marsh was born on 3 April 1985.")
    assert result.outcome == "OK"


def test_public_role_singer_birthday_does_not_warn():
    result = reid.assess("The singer Petra Hale was born on 12 June 1990.")
    assert result.outcome == "OK"


def test_business_street_address_is_not_personal():
    text = "Our office at 9 Bellflower Lane, Dunmere is open late."
    assert reid.assess(text).outcome == "OK"


def test_business_phone_and_email_are_not_personal():
    text = ("Headquarters at Port Meridian; contact us at info@corvane.io "
            "or 555-0142.")
    assert reid.assess(text).outcome == "OK"


def test_named_business_with_suffix_is_not_personal():
    assert reid.assess("Harborlight Logistics Ltd opened a new depot.").outcome == "OK"


def test_business_sales_email_is_not_personal():
    assert reid.assess("Email sales@corvane.io to book the hall.").outcome == "OK"


def test_aggregate_staff_age_does_not_count():
    assert reid.assess("Staff aged 30 to 40 at the clinic prefer nights.").outcome == "OK"


def test_aggregate_people_born_does_not_count():
    assert reid.assess("People born in 1990 visit the clinic each spring.").outcome == "OK"


def test_aggregate_percentage_does_not_count():
    assert reid.assess("60% of customers are aged 25 to 34 this year.").outcome == "OK"


def test_jobtitle_plus_town_alone_is_ok():
    assert reid.assess("Our pharmacist in Dunmere retired.").outcome == "OK"


def test_no_double_count_street_as_name():
    score, kinds, outcome = reid.score_message(
        reid.extract_quasi_identifiers("Parcel to 2200 Harbor Pike."))
    assert kinds == ["street"]
    assert outcome == "OK"


def test_no_double_count_date_as_dob_and_event():
    score, kinds, outcome = reid.score_message(
        reid.extract_quasi_identifiers("Born 14 March 1989."))
    assert kinds == ["dob"]
    assert outcome == "OK"


def test_generalise_dob_reads_born_in_year():
    text = "Mara Vell, born 14 March 1989."
    new_text, _ = reid.generalise(text, reid.extract_quasi_identifiers(text))
    assert "born in 1989" in new_text
    assert "14 March 1989" not in new_text


def test_generalise_birth_year_reads_born_in_year():
    text = "Tilda Renn, born in 1991."
    new_text, _ = reid.generalise(text, reid.extract_quasi_identifiers(text))
    assert "born in 1991" in new_text


def test_generalise_postcode_keeps_prefix():
    text = "Postcode 90274."
    new_text, _ = reid.generalise(text, reid.extract_quasi_identifiers(text))
    assert "902**" in new_text


def test_generalise_has_no_repeated_articles():
    texts = [
        "Mara Vell, born 14 March 1989, of 9 Bellflower Lane, Dunmere.",
        "The pharmacist at the Fairhaven clinic retired.",
        "She lives at 12 Dyer's Court, Gullhaven.",
    ]
    for text in texts:
        new_text, _ = reid.generalise(text, reid.extract_quasi_identifiers(text))
        assert "the the" not in new_text.lower()
        assert "a a" not in new_text.lower()


def test_generalise_initials_only_for_person_names():
    text = "Mara Vell, born 14 March 1989."
    new_text, _ = reid.generalise(text, reid.extract_quasi_identifiers(text))
    assert "M.V." in new_text
    redacted = "Patient [PII_NAME_1] was seen."
    redacted_new, _ = reid.generalise(
        redacted, reid.extract_quasi_identifiers(redacted))
    assert "[PII_NAME_1]" in redacted_new
