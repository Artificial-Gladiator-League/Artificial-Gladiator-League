from django.db import migrations

# Hard-coded on purpose: a migration must not depend on current model constants.
SLUG = "gladiatormania-india"
VERSION = "1.0"
TITLE = "The Gladiatormania (India) \u2014 Terms & Conditions"

# DRAFT. Derived from the Gladiatormania v1.0 text. It is created INACTIVE so it cannot be
# attached to a tournament until it has been reviewed (legal review included) and activated.
BODY = r"""      <p class="font-semibold text-gray-200 mb-1">The Gladiatormania (India) &mdash; Terms &amp; Conditions</p>
      <p><strong>Organizer:</strong> Artificial Gladiator League (AGL), Rishon LeZion, Israel</p>
      <hr class="border-gray-600">
      <p class="font-semibold text-gray-200 mb-1">Key Points (Summary)</p>
      <ul class="list-disc list-inside space-y-1">
        <li>The Gladiatormania is a <strong>single-elimination AG tournament in chess</strong> &mdash; lose one match and you are out. This edition is open to <strong>residents of India</strong>.</li>
        <li><strong>Entry is free.</strong> AGL never charges an entry fee, credits, tokens or any other payment to enter or to use any feature of the Tournament. No purchase is necessary.</li>
        <li><strong>Eligibility:</strong> you must be at least <strong>18 years old</strong> and a <strong>resident of India</strong> with a valid AGL account.</li>
        <li><strong>Prize:</strong> the tournament champion receives {{ tournament.prize_amount }} {{ tournament.prize_currency }} (winner takes all). The prize is funded by AGL, fixed and published in advance, and awarded solely on performance in the Tournament.</li>
        <li><strong>Claiming:</strong> the prize must be claimed within {{ tournament.claim_deadline_days }} days, and is paid only after your eligibility has been verified.</li>
        <li><strong>Zero tolerance for cheating</strong> &mdash; including changing your model&rsquo;s repository mid-tournament &mdash; results in immediate disqualification and forfeiture of prizes.</li>
        <li><strong>Zero tolerance for Violence:</strong> any form of violent behavior, threats, or harassment is strictly prohibited and may result in immediate disqualification and ban from every activity on the platform.</li>
        <li>AGL may cancel, postpone, or adjust the schedule or any other feature of the tournament with reasonable notice.</li>
        <li>Disputes are governed by <strong>Israeli law</strong>, under the exclusive jurisdiction of the <strong>courts of Rishon LeZion</strong>, without affecting any mandatory rights you have under the law of your country of residence.</li>
      </ul>
      <hr class="border-gray-600">
      <p class="font-semibold text-gray-200 mb-1">1. Definitions</p>
      <p>1.1 <strong>&ldquo;AGL&rdquo; / &ldquo;Artificial Gladiator League&rdquo; / &ldquo;we&rdquo; </strong> means The Gladiatormania tournament operator, based in Rishon LeZion, Israel.</p>
      <p>1.2 <strong>&ldquo;AG Champion&rdquo;</strong> means The Artificial Gladiator Champion, and its associated repository, that a participant submits to compete on their behalf.</p>
      <p>1.3 <strong>&ldquo;Participant&rdquo; / &ldquo;you&rdquo;</strong> means any individual who registers for and/or competes in The Gladiatormania with his AG Champion.</p>
      <p>1.4 <strong>&ldquo;Tournament&rdquo;</strong> means a single instance of The Gladiatormania open to residents of India.</p>
      <p>1.5 <strong>&ldquo;Single-Elimination&rdquo;</strong> means a bracket tournament format in which a Participant who loses a match is eliminated from the Tournament and does not compete in any further round; a Participant who wins advances to face another winning Participant in the next round, until one Participant remains undefeated as champion.</p>
      <p>1.6 <strong>&ldquo;Platform&rdquo;</strong> means the AGL website and associated services through which the Tournament is operated.</p>
      <p>1.7 <strong>&ldquo;Prize&rdquo;</strong> means the amount stated in Section 5.1.</p>
      <hr class="border-gray-600">
      <p class="font-semibold text-gray-200 mb-1">2. Eligibility &amp; Registration</p>
      <p>2.1 To participate in The Gladiatormania, you must:</p>
      <p class="pl-4">a. Be at least <strong>18 years of age</strong> at the time of registration;</p>
      <p class="pl-4">b. Be a <strong>resident of India</strong>, and confirm this when you register;</p>
      <p class="pl-4">c. Hold a valid, active AGL account in good standing; and</p>
      <p class="pl-4">d. Agree to comply with these T&amp;C and all other applicable AGL platform terms and policies.</p>
      <p>2.2 AGL may request information reasonably necessary to verify your identity, age, or residency eligibility, and may suspend or deny entry or payment of a Prize pending such verification. Section 5.3 describes what may be asked of a winner.</p>
      <p>2.3 AGL reserves the right, at its reasonable discretion, to refuse or revoke registration for any Participant who does not meet the eligibility criteria in this Section 2, or who has previously violated these T&amp;C.</p>
      <p>2.4 Registration for a given Tournament is subject to the entry window, capacity limits, and any other requirements published on the Tournament page.</p>
      <p>2.5 <strong>Free entry.</strong> Participation is free. AGL does not charge, and will not ask you for, any entry fee, deposit, stake, credits, tokens or other payment to register, to compete, or to be eligible for the Prize, and no purchase of any kind is necessary.</p>
      <p>2.6 <strong>Skill only.</strong> The Prize is awarded solely on the basis of performance: the results of matches played by Participants&rsquo; AG Champions under Section 3. It does not depend on chance, a draw or a lottery.</p>
      <hr class="border-gray-600">
      <p class="font-semibold text-gray-200 mb-1">3. Tournament Format &amp; Rules</p>
      <p>3.1 <strong>Format.</strong> The Gladiatormania is run under a <strong>single-elimination</strong> bracket. A Participant who loses a match (including any Armageddon tiebreak under Section 3.7) is eliminated from that Tournament and does not compete in any further round. The Tournament concludes when one undefeated Participant remains as champion.</p>
      <p>3.2 <strong>Game type.</strong> Matches are played by AG models submitted by Participants, in <strong>chess</strong>.</p>
      <p>3.3 <strong>Time control.</strong> Each match is played under the time control specified on the Tournament page for that Tournament.</p>
      <p>3.4 <strong>Pairings and scoring.</strong> Round pairings, scoring, and standings are generated and maintained by AGL&rsquo;s tournament system. AGL&rsquo;s determination of pairings, results, and final standings is final, save for manifest error or a successful dispute under Section 8.</p>
      <p>3.5 <strong>Model conduct during matches.</strong> Your AG Champion   must compete as submitted at the time your Tournament registration is finalized. See Section 7 for restrictions on changes during an active Tournament.</p>
      <p>3.6 <strong>Time per move.</strong> Before joining a Tournament, the Participant selects a target "time per move" (AG Champion thinking time) setting for their AG Champion, as offered on the registration page. This setting reflects a target pace only; actual response time per move may vary depending on how the opposing Participant's model is hosted, and AGL does not guarantee that any match will complete within a specific duration. AGL may apply a reasonable maximum time limit per move or per match, at its discretion, to ensure Tournament matches complete in a timely manner.</p>
      <p>3.7 <strong>Drawn games.</strong> If a match ends in a draw, it is automatically resolved by a single additional Armageddon game between the same two Participants under an accelerated time control, with colours reassigned at random. In the Armageddon game, the Participant playing Black wins if that game is also drawn ("draw odds"). The result of the Armageddon game determines who advances and who is eliminated for purposes of Section 3.1.</p>
      <hr class="border-gray-600">
      <p class="font-semibold text-gray-200 mb-1">4. Schedule &amp; Changes</p>
      <p>4.1 The Tournament is held on the date and time published on the Tournament page.</p>
      <p>4.2 AGL may adjust the day, time, or duration of the Tournament, provided that reasonable notice is given to registered Participants.</p>
      <p>4.3 AGL reserves the right to cancel or postpone any Tournament due to technical issues, an insufficient number of participants, or any other reasonable operational cause. In such cases, AGL will make reasonable efforts to notify affected Participants and, where applicable, reschedule the Tournament or address any prize implications.</p>
      <p>4.4 AGL reserves the right, at its sole discretion, to increase the Prize for a given Tournament, without any obligation to do so and without this establishing any expectation of an increased Prize in future Tournaments. AGL will not reduce the published Prize once registration has opened, except where the Tournament is cancelled under Section 4.3.</p>
      <hr class="border-gray-600">
      <p class="font-semibold text-gray-200 mb-1">5. Prize &amp; Payment</p>
      <p>5.1 <strong>Prize amount.</strong> This Tournament is <strong>winner-take-all</strong>: the sole prize is <strong>{{ tournament.prize_amount }} {{ tournament.prize_currency }}</strong>, funded by AGL, fixed and published in advance, and awarded to the Tournament champion (the single undefeated Participant remaining under Section 3.1). No prize is paid to any other Participant.</p>
      <p>5.2 <strong>Payment.</strong> The Prize is paid in {{ tournament.prize_currency }} by bank transfer, UPI or another payment method arranged with you by AGL (not PayPal). AGL will contact you at the email address of your AGL account to arrange the payment. <strong>Never send bank, UPI or card details through the Platform</strong>; AGL does not collect them there.</p>
      <p>5.3 <strong>Claiming and verification.</strong> To receive the Prize, the winner must claim it through the Platform and complete the identity, age and residency verification requested by AGL. AGL may ask to see:</p>
      <ul class="list-disc list-inside pl-4 space-y-1">
        {% for document in tournament.verification_documents_list %}<li>{{ document }}</li>{% endfor %}
      </ul>
      <p>AGL does not request your Aadhaar number or Aadhaar card; please do not send it. AGL records on the Platform only that a check took place, who performed it and when, and not the numbers on your documents.</p>
      <p>5.4 <strong>Taxes.</strong> The Prize is subject to any applicable taxes, withholdings, or other legal requirements, including under Indian law. Where the law requires, AGL may deduct tax from the Prize before payment and will tell you the amount withheld. Each Participant is solely responsible for any tax obligations arising from a Prize they receive.</p>
      <p>5.5 A Participant who is disqualified under Section 8, or who is later found ineligible under Section 2, forfeits any right to the Prize, and AGL may reallocate or withhold the Prize accordingly.</p>
      <p>5.6 <strong>Claim deadline.</strong> The winner must complete the steps that are theirs to take within <strong>{{ tournament.claim_deadline_days }} days</strong> of being notified that they have won. The period counts only while the next step is the winner&rsquo;s: it is paused while AGL is verifying eligibility or arranging payment, and while the Prize is on hold under Section 5.7. A Prize that is not claimed within the period is forfeited.</p>
      <p>5.7 <strong>Holds.</strong> AGL may suspend payment of the Prize while it completes verification, investigates a suspected breach of these T&amp;C, or where the law requires. If this happens, AGL will tell you that the Prize is on hold.</p>
      <hr class="border-gray-600">
      <p class="font-semibold text-gray-200 mb-1">6. Code of Conduct &amp; Fair Play</p>
      <p>6.1 All Participants must treat every other participants, AGL staff, and any other person with respect at all times.</p>
      <p>6.2 Harassment, discrimination, hate speech, and abusive behavior of any kind are strictly prohibited, whether directed at another participant, AGL, or any third party.</p>
      <p>6.3 There is no place on the platform for violence, threats of violence, or incitement to violence of any kind.</p>
      <p>6.4 Cheating of any kind is strictly prohibited. Cheating includes, without limitation, the conduct described in Section 7.</p>
      <p>6.5 Violation of this Section 6 may result in disqualification, suspension, or termination of a Participant&rsquo;s account, in accordance with Section 8.</p>
      <hr class="border-gray-600">
      <p class="font-semibold text-gray-200 mb-1">7. Anti-Cheating &amp; Integrity</p>
      <p>7.1 <strong>Repository changes during a Tournament.</strong> Changing your AG Champion's Model&rsquo;s repository, or the contents thereof, at any point during an active Tournament is considered cheating and will result in <strong>immediate disqualification</strong> from that Tournament.</p>
      <p>7.2 Without limiting Section 7.1, AGL may also disqualify a Participant for:</p>
      <p class="pl-4">a. Manipulating a Model, its repository, or its inference endpoint during a Tournament;</p>
      <p class="pl-4">b. Exploiting bugs, defects, or vulnerabilities in the Platform to gain an unfair advantage; or</p>
      <p class="pl-4">c. Any other violation of this Section 7, Section 6 (Code of Conduct), or these T&amp;C generally.</p>
      <p>7.3 AGL may use automated or manual integrity checks to monitor compliance with this Section 7. Participants agree to cooperate with any reasonable request from AGL related to such checks.</p>
      <p>7.4 <strong>Repository changes in proximity to a Tournament.</strong> Changing, modifying, or replacing the Model's repository, or any contents thereof, during the period immediately preceding the scheduled start time of a Tournament for which the Participant is registered, shall likewise be deemed cheating for purposes of this Section 7, and shall result in the immediate disqualification of the Participant from that Tournament, irrespective of whether such change is detected prior to, during, or following the Tournament.</p>
      <hr class="border-gray-600">
      <p class="font-semibold text-gray-200 mb-1">8. Disqualification &amp; Sanctions</p>
      <p>8.1 AGL may disqualify a Participant from a Tournament, at AGL&rsquo;s reasonable discretion, for any violation of Section 6 (Code of Conduct) or Section 7 (Anti-Cheating &amp; Integrity), or of these T&amp;C more generally.</p>
      <p>8.2 A disqualified Participant forfeits all prize eligibility for the Tournament in which the disqualification occurs.</p>
      <p>8.3 AGL may, in addition to disqualification from a specific Tournament, suspend or terminate a Participant&rsquo;s AGL account for repeated or serious violations.</p>
      <p>8.4 A Participant who believes they were disqualified or sanctioned in error may raise the matter with AGL through the contact channel in Section 13. AGL will review such disputes in good faith, but its determination following review shall be final.</p>
      <hr class="border-gray-600">
      <p class="font-semibold text-gray-200 mb-1">9. Data Protection &amp; Email Usage</p>
      <p>9.1 AGL will collect and store your email address and will use it only for the following purposes:</p>
      <p class="pl-4">a. Account registration and authentication on the AGL Platform;</p>
      <p class="pl-4">b. Password reset and account recovery;</p>
      <p class="pl-4">c. Communications related to your Tournament participation, including notifications and results; and</p>
      <p class="pl-4">d. Verifying eligibility, and arranging and paying the Prize.</p>
      <p>9.2 AGL will not sell Participants&rsquo; email addresses to third parties.</p>
      <p>9.3 AGL may process other personal data reasonably necessary to operate the Platform and to comply with applicable law, including the data protection law that applies to you.</p>
      <p>9.4 Participants may contact AGL using the details in Section 13 with questions or requests regarding their personal data.</p>
      <hr class="border-gray-600">
      <p class="font-semibold text-gray-200 mb-1">10. Limitation of Liability</p>
      <p>10.1 The Platform and Tournament are provided on an &ldquo;as is&rdquo; and &ldquo;as available&rdquo; basis. AGL does not guarantee uninterrupted or error-free operation of the Platform or any Tournament.</p>
      <p>10.2 To the maximum extent permitted by applicable law, AGL shall not be liable for any indirect, incidental, or consequential damages arising from a Participant&rsquo;s use of the Platform or participation in a Tournament, including damages arising from technical failures, cancellations, or postponements under Section 4.</p>
      <p>10.3 Nothing in this Section 10 excludes or limits any liability that cannot lawfully be excluded or limited under applicable law.</p>
      <hr class="border-gray-600">
      <p class="font-semibold text-gray-200 mb-1">11. Modifications to These Terms</p>
      <p>11.1 AGL may update or amend these T&amp;C from time to time. Material changes will be communicated to Participants by a reasonable method, such as posting an updated version on the Platform or notifying registered Participants by email.</p>
      <p>11.2 Continued participation in The Gladiatormania following the effective date of any updated T&amp;C constitutes acceptance of the updated terms.</p>
      <hr class="border-gray-600">
      <p class="font-semibold text-gray-200 mb-1">12. Governing Law &amp; Jurisdiction</p>
      <p>12.1 These T&amp;C, and any dispute arising out of or in connection with The Gladiatormania or these T&amp;C, shall be governed by the laws of the State of Israel, without regard to its conflict-of-laws principles.</p>
      <p>12.2 The competent courts of Rishon LeZion, Israel, shall have exclusive jurisdiction over any such dispute.</p>
      <p>12.3 These T&amp;C are drafted in English. Should a translation be provided for convenience, the English version shall prevail in the event of any conflict or inconsistency.</p>
      <p>12.4 Nothing in this Section 12 deprives you of any mandatory right that you have under the law of your country of residence and that cannot be waived by agreement.</p>
      <hr class="border-gray-600">
      <p class="font-semibold text-gray-200 mb-1">13. Contact Information</p>
      <p>For questions about these T&amp;C, The Gladiatormania, the Prize, or a dispute regarding disqualification, please contact AGLadiator through the contact details published on the Platform.</p>
      <hr class="border-gray-600">
"""


def seed(apps, schema_editor):
    TournamentTerms = apps.get_model("tournaments", "TournamentTerms")
    # Never overwrite an existing record: published wording is immutable.
    TournamentTerms.objects.get_or_create(
        slug=SLUG,
        version=VERSION,
        defaults=dict(
            title=TITLE,
            body=BODY,
            requires_age_18=True,
            # The field name is historical: it means "requires a residency declaration".
            requires_israeli_residency=True,
            has_prize=True,
            is_active=False,
        ),
    )


def unseed(apps, schema_editor):
    TournamentTerms = apps.get_model("tournaments", "TournamentTerms")
    Tournament = apps.get_model("tournaments", "Tournament")
    Participant = apps.get_model("tournaments", "TournamentParticipant")
    for record in TournamentTerms.objects.filter(slug=SLUG, version=VERSION):
        in_use = (
            Tournament.objects.filter(terms=record).exists()
            or Participant.objects.filter(accepted_terms_slug=SLUG, terms_version_accepted=VERSION).exists()
        )
        if not in_use:
            record.delete()


class Migration(migrations.Migration):

    dependencies = [
        ("tournaments", "0035_prize_claim_payout_workflow"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
