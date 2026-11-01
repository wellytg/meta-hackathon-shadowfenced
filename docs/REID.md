# Re-identification Risk (T30)

Scope: quasi-identifiers the redaction and trace pipeline must treat as
re-identifying in combination, even when each value looks harmless alone.
All people, places, and values below are invented.

## 1. Quasi-identifier list

| Kind | Definition | Example values (fictional) |
|---|---|---|
| Name | Full or partial personal name, including nicknames and initials | `Alicia M. Fernwood`, `J. K. Harrow` |
| Date of birth (DOB) | Exact calendar birth date | `1988-04-17` |
| Age | Age in years, exact or approximate | `37`, `about 36` |
| Street address | House number plus street name, with or without unit | `14 Bramble Lane, Apt 3`, `2200 Harbor Pike` |
| Postcode | Full postal / ZIP code | `90274`, `SW1A 2AA` (invented use) |
| City | Town, city, or locality of residence, work, or study | `Port Meridian`, `Kingsbury` |
| Employer | Named current or former employer | `Harborlight Logistics`, `Corvane Labs` |
| Job title | Exact role or rank | `Senior Payroll Analyst`, `Night-shift Triage Nurse` |
| School | Named school, college, or university attended | `Westmere Secondary College`, `Kingsbury Technical Institute` |
| Gender | Stated gender or gendered title/pronoun | `female`, `Mr.` / `she/her` |
| Distinctive medical condition | Rare diagnosis, device, or treatment that narrows the pool | `early-onset Huntington's disease`, `left-ventricular assist device` |
| Vehicle plate | Full or partial registration plate | `8XQT-441 (fictional)`, `partial plate ending 441` |
| Unique dated events | One-off dated milestones: graduations, awards, accidents, record performances | `graduated 2011-06-12`, `ran the 2019 Port Meridian marathon in 2:41:03` |
| Phone (contact anchor) | Full or partial phone number; acts as a join key | `555-014-2288` |
| Email (contact anchor) | Personal email address; acts as a join key | `alicia.fernwood@example.com`, `jk.harrow@corvane.io` |

Notes:

- Contact anchors (phone, email) are direct identifiers first and
  quasi-identifiers second: even a fragment supports linkage.
- "Unique dated events" covers anything with a date plus a rare fact
  (award, incident, publication, athletic record). One such event can be
  as identifying as a name.

## 2. Why combinations matter

Single-field redaction is not enough. Re-identification comes from the
intersection of fields.

- **k-anonymity.** A released record satisfies k-anonymity when at least
  k people share its quasi-identifier tuple. Dropping the name while
  leaving DOB + postcode + gender often leaves k = 1: the tuple itself
  is the identifier. Generalisation (Section 4) raises k by coarsening
  each field until the tuple is shared by many people.
- **Linkage attacks.** An adversary joins the redacted trace against an
  outside table (voter roll, directory, breach dump, roster) on shared
  quasi-identifiers. The trace need not contain a name: matching
  DOB + postcode + employer against a public staff list can resolve the
  row to one person.
- **OSINT inference.** Searchable fragments compose: a job title plus a
  city narrows to an employer; an employer plus a graduation year narrows
  to a cohort; a dated marathon time resolves the rest. Each clue looks
  benign; the search engine performs the join.
- **Residual clues after redaction.** Placeholders remove values but not
  shape: counts (`[PII_PHONE_1]` vs `[PII_PHONE_2]`), ordering, nearby
  unredacted context ("our night-shift triage nurse"), and repeated
  placeholders across turns all leak structure. Distinctive conditions
  and unique dated events are especially leaky because generalising the
  common fields still leaves a one-of-a-kind fact behind.
- **Canonical result.** Sweeney (2000) showed 87% of the US population
  is uniquely identified by just {5-digit ZIP, gender, date of birth}.
  Treat DOB + postcode + gender as identifying by default, not as three
  harmless demographics.

## 3. High-risk combinations

| Combination | Rationale |
|---|---|
| Name + DOB | Direct join key: resolves against almost any roster, record, or directory |
| Name + street address | Pinpoints household; supports impersonation and physical-safety harm |
| DOB + postcode + gender | Sweeney triple: ~87% unique in the US; identify without any name |
| Employer + job title + city | Narrows to a team of one in small firms; searchable on company sites |
| School + graduation year + city | Cohort of a few dozen; alumni lists and photos finish the join |
| Distinctive medical condition + city | Rare condition in one place is k = 1 even with no demographics |
| Vehicle plate + city | Plate registries, photos, and tow/parking posts resolve the owner |
| Employer + unique dated event | "Won the 2022 Harborlight safety award" singles out one employee |
| Age + postcode + employer | Coarse age still joins against a staff directory filtered by area |
| Job title + school + graduation year | Rare role plus cohort: LinkedIn-style search returns one profile |

Rule of thumb: any pair containing a name, plate, phone (`555-014-2288`),
or email (`jk.harrow@corvane.io`) is high risk; any triple of demographics,
work, or education facts is high risk; any single distinctive condition or
unique dated event plus one locator (city, postcode, employer) is high risk.

## 4. Generalisation table

Coarsen before release. Each row preserves analytic utility while raising k.

| Kind | Raw (never release) | Generalised | Utility preserved |
|---|---|---|---|
| DOB | `1988-04-17` | Birth year `1988` | Age-cohort analysis |
| Age | `37` | Decade `30s` | Band-level stats |
| Street address | `14 Bramble Lane, Apt 3` | City `Port Meridian` | Regional routing |
| Postcode | `90274` / `SW1A 2AA` | First 3 chars `902**` / `SW1*` | Delivery-zone level |
| City | `Port Meridian` | Region `North Coast region` | Regional aggregates |
| Employer | `Harborlight Logistics` | Sector `logistics firm` | Industry analysis |
| Job title | `Senior Payroll Analyst` | Job family `finance / payroll staff` | Workforce mix |
| School | `Westmere Secondary College` | School type `secondary school` | Education level |
| Gender | `female` | Suppress unless needed for the task | Avoids Sweeney triple |
| Medical condition | `early-onset Huntington's disease` | Category `neurological condition` | Prevalence band |
| Vehicle plate | `8XQT-441 (fictional)` | Suppress or `private vehicle` | Mode of transport only |
| Unique dated event | `2019 Port Meridian marathon, 2:41:03` | `completed a marathon in 2019` | Athletic-participation flag |
| Phone | `555-014-2288` | Suppress or `phone on file: yes/no` | Contactability flag |
| Email | `alicia.fernwood@example.com` | Domain type `personal email` | Channel flag |

Apply generalisation cumulatively: a record keeping birth year, 3-char
postcode, and job family is far safer than one keeping all three raw
values, and suppress the third field when the first two already narrow
the pool.

## 5. Local-only handling note

Raw quasi-identifier values stay local. Detection, placeholder mapping
(`[PII_PHONE_1]`, `[CLIENT_1]`, and friends), and re-hydration run
against the in-memory map on the originating side only (15-minute TTL,
never logged, never persisted). Outbound traces carry labels and
fingerprints, not raw values: span class/subclass (`pii/phone`,
`pii/address`), generalised bands (`birth year`, `postcode prefix`),
match counts, and salted hashes for join detection. Any reviewer,
log viewer, or downstream service sees `DOB generalised to birth year`
rather than `1988-04-17`.

## 6. False-warning guards (T30b)

The detector must stay quiet on messages that cannot identify anyone.
These rules are implemented in `reid.py` and take precedence over the
weights in section 3:

- **One detail alone never warns.** A single quasi-identifier kind,
  however identifying it looks on its own (a full date of birth, a
  street address, a plate), scores below the warning line. It takes at
  least two distinct kinds to warn.
- **A birth year or an age is never a date of birth.** Year-only birth
  references (`born 1991`, `born in 1991`) score as age, and bare years
  (`1991`) score as dated events. Only full calendar dates count as
  DOB, so a birth year or an age can never combine as name-plus-birthday.
  A lone first name is likewise not a name: names need a pair, a
  title, or a redaction token.
- **Organisation details are not personal details.** Contact details
  attached to an organisation -- company names carrying Ltd/Inc/LLC/
  Clinic/Pty, `our office at`, `headquarters`, `contact us` -- do not
  count as a private person's street, phone, email, postcode, or
  employer finding. Role inboxes (`info@`, `sales@`, `support@` and
  friends) are never a personal email address.
- **Group statements do not count.** Age, birthday, and dated-event
  findings near aggregate or plural phrasing (`people`, `staff aged`,
  `customers born`, percentages) describe a group, not one person,
  and are dropped before scoring.
- **Public roles lighten birthdays.** A public-role indicator (`the
  actor`, `the senator`, `the singer`, `CEO of`) removes the birthday's
  weight: a widely published birthday in a public role is not a private
  date of birth. Stronger public-figure cases stay fully exempt.
- **A job title plus a town is not enough.** The exact pair of job
  title + city, with no name or employer, never warns.
- **One span counts once.** A street name that also matches a person's
  name scores as street only; a birth date that also matches a dated
  event scores as date of birth only. Generalisation redacts the same
  winning span once.

## 6b. False-warning guards (T30c)

A second pass over safe messages that still warned. Each rule below is
implemented in `reid.py` and covered by `tests/test_t30c_reid.py`:

- **Pronouns are not gender.** A gender quasi-identifier needs an
  explicit statement (`female`, `gender: M`, `a 34-year-old man`).
  Bare pronouns (`he`, `she`, `him`, `her`, `his`) never score as
  gender, so `She is 34 and lives in Dunmere` stays quiet while the
  Sweeney triple with `female` still holds.
- **A lone first name is not a name.** Message verbs pair with the
  addressee (`Tell Jake I said hi`, `Ask Priya to call back`); such
  verb-led pairs never count as a name quasi-identifier. Full names
  (`Mara Vell, born ...`) still hold.
- **Public-figure markers exempt name plus birthday.** `pop star`,
  `actor`, `(public figure)`, `senator`, `the band's singer`, and
  `all over the press` drop the birthday's weight in the same message
  or anywhere in the conversation, so a published birthday in that
  frame never warns. Any other detail (a street, a phone number) still
  counts as usual.
- **Shop-front details belong to the business.** Trade nouns
  (`shop`, `store`, `brewery`, `café`, `restaurant`, `clinic`,
  `office`, `studio`), `our shop`, opening-hours notes (`Open
  Mon–Sat`), `for bookings`, and `trade enquiries` mark a nearby
  street, postcode, phone number, or email as the business's, not a
  person's.
- **Job adverts describe a role.** `we need a`, `we are hiring`,
  `apply to`, `vacancy`, `experience level`, and role-based mailboxes
  (`jobs@`, `careers@`, `hr@`, `tours@`, `bookings@`, `hello@`) mean a
  nearby job title and town name the open role, not a person.
- **Group statistics are not personal.** People counts (`42
  patients`), age bands (`aged 30–39`), percentages (`60% female`),
  averages, and area codes used for a whole area never score as one
  person's age, gender, date of birth, or postcode.
- **A singleton role plus a town still warns.** `only night-shift
  dispatcher at Port Aubrun` names a team of one, so unlike a generic
  `pharmacist in Dunmere` it stays flaggable even with no name.

Generalised text must read naturally: birth dates become `born in
1991` (never a bare year that loses the sense), streets generalise to
their city without duplicating it (`of 9 Bellflower Lane, Dunmere`
becomes `of Dunmere`), postcodes keep their prefix (`postcode
982**`), neighbouring articles collapse (never `the the`), and person
names become initials (`M.V.`) only when they are a person's name --
already-redacted tokens such as `[PII_NAME_1]` are left untouched.
