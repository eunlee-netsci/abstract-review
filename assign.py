"""Conflict-of-interest detection and balanced automatic reviewer assignment."""
import math
import random
import re

# Webmail domains: sharing these says nothing about a conflict of interest.
GENERIC_DOMAINS = {
    "gmail.com", "googlemail.com", "naver.com", "daum.net", "hanmail.net", "kakao.com",
    "nate.com", "outlook.com", "hotmail.com", "live.com", "yahoo.com", "yahoo.co.kr",
    "icloud.com", "me.com", "proton.me", "protonmail.com", "qq.com", "163.com", "126.com",
}


def _norm(s):
    return re.sub(r"[^0-9a-z가-힣]", "", (s or "").lower())


def _domain(email):
    email = (email or "").lower()
    return email.split("@", 1)[1] if "@" in email else ""


def has_coi(reviewer, submission):
    """Return a short reason string if the reviewer has a conflict with the submission, else None."""
    if submission.owner_id == reviewer.id:
        return "own submission"
    r_email = reviewer.email.lower()
    r_dom = _domain(r_email)
    r_aff = _norm(reviewer.affiliation)
    people = list(submission.authors)
    if submission.owner is not None:
        people.append({"email": submission.owner.email, "affiliation": submission.owner.affiliation})
    for a in people:
        a_email = (a.get("email") or "").lower()
        if a_email and a_email == r_email:
            return "co-author"
        if len(r_aff) >= 3 and _norm(a.get("affiliation")) == r_aff:
            return "same affiliation"
        a_dom = _domain(a_email)
        if r_dom and a_dom == r_dom and r_dom not in GENERIC_DOMAINS:
            return "same email domain"
    return None


def keyword_overlap(reviewer, submission):
    rk = {k.lower() for k in reviewer.keyword_list}
    sk = {k.lower() for k in submission.keyword_list}
    return len(rk & sk)


def auto_assign(submissions, reviewers, assigned_all, assigned_active, k, seed=2028):
    """Greedy balanced assignment.

    assigned_all:    {submission_id: set(reviewer_id)} including declared conflicts (never re-assign these)
    assigned_active: {submission_id: set(reviewer_id)} excluding conflicts (count toward k and load)

    Papers with the fewest eligible reviewers are filled first. For each paper, reviewers below the
    load cap are preferred, then higher keyword overlap, then lower current load.
    Returns (new_pairs, unfilled_submissions, cap).
    """
    rng = random.Random(seed)
    if not reviewers:
        return [], [s for s in submissions], 0

    load = {r.id: 0 for r in reviewers}
    for rids in assigned_active.values():
        for rid in rids:
            if rid in load:
                load[rid] += 1

    need = {s.id: max(0, k - len(assigned_active.get(s.id, ()))) for s in submissions}
    total = sum(load.values()) + sum(need.values())
    cap = math.ceil(total / len(reviewers))

    eligible = {
        s.id: [r for r in reviewers
               if r.id not in assigned_all.get(s.id, set()) and not has_coi(r, s)]
        for s in submissions
    }
    tiebreak = {r.id: rng.random() for r in reviewers}
    order = sorted((s for s in submissions if need[s.id] > 0),
                   key=lambda s: (len(eligible[s.id]), rng.random()))

    new_pairs, unfilled = [], []
    for s in order:
        cands = sorted(eligible[s.id],
                       key=lambda r: (load[r.id] >= cap, -keyword_overlap(r, s),
                                      load[r.id], tiebreak[r.id]))
        picked = cands[:need[s.id]]
        for r in picked:
            load[r.id] += 1
            new_pairs.append((s.id, r.id))
        if len(picked) < need[s.id]:
            unfilled.append(s)
    return new_pairs, unfilled, cap
