from django.db import migrations

TERMS_TEXT = """\
AGL\u2122, Gladiate\u2122, Lets Gladiate\u2122, Artificial Gladiator\u2122, and Artificial Gladiator League\u2122 are trademarks of AGLadiator.

Gladiator Gauntlet \u2014 Terms & Conditions

Organizer: AGLadiator (AGL), Rishon LeZion, Israel

These Terms & Conditions (\u201cT&C\u201d) are not legal advice. If you are unsure about your rights or obligations under this document, you may wish to consult an independent lawyer.

---

Key Points (Summary)

- The Gladiator Gauntlet is a weekly, Swiss-format AI tournament (chess and/or breakthrough) \u2014 no elimination, everyone plays every round.
- Eligibility: you must be at least 18 years old and a resident of Israel with a valid AGL account.
- Prizes: 1st place \u20aa100, 2nd place \u20aa50, 3rd place \u20aa25, paid in NIS, subject to tax and identity/payment information requirements.
- Zero tolerance for cheating \u2014 including changing your model\u2019s repository mid-tournament \u2014 results in immediate disqualification and forfeiture of prizes.
- AGL may cancel, postpone, or adjust the schedule with reasonable notice.
- Disputes are governed by Israeli law, under the exclusive jurisdiction of the courts of Rishon LeZion.

---

1. Definitions

1.1 \u201cAGL\u201d / \u201cAGLadiator\u201d / \u201cwe\u201d / \u201cus\u201d means the Gladiator Gauntlet tournament operator, based in Rishon LeZion, Israel.

1.2 \u201cParticipant\u201d / \u201cyou\u201d means any individual who registers for and/or competes in the Gladiator Gauntlet.

1.3 \u201cTournament\u201d means a single weekly instance of the Gladiator Gauntlet.

1.4 \u201cModel\u201d means the AI agent, and its associated repository, that a Participant submits to compete on their behalf.

1.5 \u201cSwiss System\u201d means a multi-round tournament format in which no participant is eliminated; each round, participants are paired against others with a similar running score, and final standings are determined by cumulative score (and tiebreakers) after all rounds are complete.

1.6 \u201cPlatform\u201d means the AGL website and associated services through which the Tournament is operated.

---

2. Eligibility & Registration

2.1 To participate in the Gladiator Gauntlet, you must:

  a. Be at least 18 years of age at the time of registration;

  b. Be a resident of the State of Israel;

  c. Hold a valid, active AGL account in good standing; and

  d. Agree to comply with these T&C and all other applicable AGL platform terms and policies.

2.2 AGL may request information reasonably necessary to verify your identity, age, or residency eligibility, and may suspend or deny entry pending such verification.

2.3 AGL reserves the right, at its reasonable discretion, to refuse or revoke registration for any Participant who does not meet the eligibility criteria in this Section 2, or who has previously violated these T&C.

2.4 Registration for a given weekly Tournament is subject to the entry window, capacity limits, and any other requirements published on the Tournament page.

---

3. Tournament Format & Rules

3.1 Format. The Gladiator Gauntlet is run under the Swiss system. Participants are not eliminated after a loss; instead, each round they are paired against opponents with a comparable score, and final rankings are determined by total score across all rounds, with tiebreakers applied as needed to resolve ties.

3.2 Game type. Matches are played by AI models submitted by Participants, in chess and/or breakthrough, as specified on the Tournament page for that week.

3.3 Time control. Each match is played under the time control specified on the Tournament page for that week\u2019s Tournament.

3.4 Pairings and scoring. Round pairings, scoring, and standings are generated and maintained by AGL\u2019s tournament system. AGL\u2019s determination of pairings, results, and final standings is final, save for manifest error or a successful dispute under Section 8.

3.5 Model conduct during matches. Your Model must compete as submitted at the time your Tournament registration is finalized. See Section 7 for restrictions on changes during an active Tournament.

---

4. Schedule & Changes

4.1 The Gladiator Gauntlet is held once per week.

4.2 AGL may adjust the day, time, or duration of a given week\u2019s Tournament, provided that reasonable notice is given to registered Participants.

4.3 AGL reserves the right to cancel or postpone any Tournament due to technical issues, an insufficient number of participants, or any other reasonable operational cause. In such cases, AGL will make reasonable efforts to notify affected Participants and, where applicable, reschedule the Tournament or address any prize implications.

---

5. Prizes & Payment

5.1 Prize amounts for each weekly Gladiator Gauntlet Tournament are as follows, unless otherwise stated on the Tournament page:

  - 1st place: \u20aa100 (100 NIS)
  - 2nd place: \u20aa50 (50 NIS)
  - 3rd place: \u20aa25 (25 NIS)

5.2 Prizes are paid in New Israeli Shekels (NIS), via a payment method specified by AGL (which may include, for example, bank transfer or PayPal).

5.3 To receive a prize, a winning Participant may be required to provide additional information, such as a PayPal email address or bank account details, and to complete any identity or eligibility verification requested by AGL.

5.4 Prizes are subject to any applicable taxes, withholdings, or other legal requirements under Israeli law. Each Participant is solely responsible for any tax obligations arising from a prize they receive.

5.5 A Participant who is disqualified under Section 8, or who is later found ineligible under Section 2, forfeits any right to a prize for the relevant Tournament, and AGL may reallocate or withhold the prize accordingly.

5.6 Unclaimed prizes may be forfeited if the winning Participant fails to provide required payment or verification information within a reasonable period specified by AGL.

---

6. Code of Conduct & Fair Play

6.1 All Participants must treat every other Participant, AGL staff, and any other person with respect at all times.

6.2 Harassment, discrimination, hate speech, and abusive behavior of any kind are strictly prohibited, whether directed at another Participant, AGL, or any third party.

6.3 There is no place on the Platform for violence, threats of violence, or incitement to violence of any kind.

6.4 Cheating of any kind is strictly prohibited. Cheating includes, without limitation, the conduct described in Section 7.

6.5 Violation of this Section 6 may result in disqualification, suspension, or termination of a Participant\u2019s account, in accordance with Section 8.

---

7. Anti-Cheating & Integrity

7.1 Repository changes during a Tournament. Changing your Model\u2019s repository, or the contents thereof, at any point during an active Tournament is considered cheating and will result in immediate disqualification from that Tournament.

7.2 Without limiting Section 7.1, AGL may also disqualify a Participant for:

  a. Manipulating a Model, its repository, or its inference endpoint during a Tournament;

  b. Exploiting bugs, defects, or vulnerabilities in the Platform to gain an unfair advantage; or

  c. Any other violation of this Section 7, Section 6 (Code of Conduct), or these T&C generally.

7.3 AGL may use automated or manual integrity checks to monitor compliance with this Section 7. Participants agree to cooperate with any reasonable request from AGL related to such checks.

---

8. Disqualification & Sanctions

8.1 AGL may disqualify a Participant from a Tournament, at AGL\u2019s reasonable discretion, for any violation of Section 6 (Code of Conduct) or Section 7 (Anti-Cheating & Integrity), or of these T&C more generally.

8.2 A disqualified Participant forfeits all prize eligibility for the Tournament in which the disqualification occurs.

8.3 AGL may, in addition to disqualification from a specific Tournament, suspend or terminate a Participant\u2019s AGL account for repeated or serious violations.

8.4 A Participant who believes they were disqualified or sanctioned in error may raise the matter with AGL through the contact channel in Section 13. AGL will review such disputes in good faith, but its determination following review shall be final.

---

9. Data Protection & Email Usage

9.1 AGL will collect and store your email address and will use it only for the following purposes:

  a. Account registration and authentication on the AGL Platform;

  b. Password reset and account recovery;

  c. Communications related to your Tournament participation, including notifications and results; and

  d. Processing and paying Tournament prizes.

9.2 AGL will not sell Participants\u2019 email addresses to third parties.

9.3 AGL may process other personal data reasonably necessary to operate the Platform and to comply with applicable law, including data protection law applicable in Israel.

9.4 Participants may contact AGL using the details in Section 13 with questions or requests regarding their personal data.

---

10. Limitation of Liability

10.1 The Platform and Tournament are provided on an \u201cas is\u201d and \u201cas available\u201d basis. AGL does not guarantee uninterrupted or error-free operation of the Platform or any Tournament.

10.2 To the maximum extent permitted by applicable Israeli law, AGL shall not be liable for any indirect, incidental, or consequential damages arising from a Participant\u2019s use of the Platform or participation in a Tournament, including damages arising from technical failures, cancellations, or postponements under Section 4.

10.3 Nothing in this Section 10 excludes or limits any liability that cannot lawfully be excluded or limited under applicable Israeli law.

---

11. Modifications to These Terms

11.1 AGL may update or amend these T&C from time to time. Material changes will be communicated to Participants by a reasonable method, such as posting an updated version on the Platform or notifying registered Participants by email.

11.2 Continued participation in the Gladiator Gauntlet following the effective date of any updated T&C constitutes acceptance of the updated terms.

---

12. Governing Law & Jurisdiction

12.1 These T&C, and any dispute arising out of or in connection with the Gladiator Gauntlet or these T&C, shall be governed by the laws of the State of Israel, without regard to its conflict-of-laws principles.

12.2 The competent courts of Rishon LeZion, Israel, shall have exclusive jurisdiction over any such dispute.

12.3 These T&C are drafted in English. Should a Hebrew translation be provided for convenience, the English version shall prevail in the event of any conflict or inconsistency.

---

13. Contact Information

For questions about these T&C, the Gladiator Gauntlet, prizes, or a dispute regarding disqualification, please contact AGLadiator through the contact details published on the Platform.

---

Disclaimer: These Terms & Conditions are provided for general informational purposes as part of AGL\u2019s tournament operations and do not constitute legal advice. Participants who have questions about their legal rights or obligations should consider consulting an independent lawyer licensed in Israel.\
"""


def set_gauntlet_terms(apps, schema_editor):
    Tournament = apps.get_model("tournaments", "Tournament")
    Tournament.objects.filter(format="gauntlet").update(
        terms_text=TERMS_TEXT,
        terms_version="1.0",
    )


def clear_gauntlet_terms(apps, schema_editor):
    Tournament = apps.get_model("tournaments", "Tournament")
    Tournament.objects.filter(format="gauntlet").update(
        terms_text="",
        terms_version="",
    )


class Migration(migrations.Migration):

    dependencies = [
        ("tournaments", "0023_rename_tournaments_eligib_status_idx_tournaments_status_3bbd6e_idx_and_more"),
    ]

    operations = [
        migrations.RunPython(set_gauntlet_terms, reverse_code=clear_gauntlet_terms),
    ]
