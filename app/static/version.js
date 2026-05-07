function versionFromUrl() {
    const m = window.location.pathname.match(/^\/v\/(.+)$/);
    return m ? decodeURIComponent(m[1]) : null;
}

function initials(name) {
    if (!name) return "";
    return name
        .trim()
        .split(/\s+/)
        .map((p) => p[0])
        .filter(Boolean)
        .join("")
        .toUpperCase();
}

const RISK_RANK = { high: 3, med: 2, low: 1 };

function riskBadge(risk) {
    const span = document.createElement("span");
    if (!risk) {
        span.className = "risk pending";
        span.textContent = "…";
        return span;
    }
    span.className = "risk risk-" + risk.level;
    span.textContent = risk.level.toUpperCase();
    span.title = (risk.reasons || []).join("\n") || "no reasons returned";
    return span;
}

function worstRiskFor(changesets) {
    let worst = 0;
    let worstRisk = null;
    for (const cs of changesets || []) {
        const lvl = cs.risk ? RISK_RANK[cs.risk.level] || 0 : 0;
        if (lvl > worst) {
            worst = lvl;
            worstRisk = cs.risk;
        }
    }
    return worstRisk;
}

// Sort buckets, highest risk first. Within a bucket, newer issue id first
// just for stability — same issues land in the same place across refreshes.
const ISSUE_BUCKET = {
    high: 5,
    med: 4,
    low: 3,
    unscored: 2,
    no_commits: 1,
};

function issueBucket(issue) {
    const worst = worstRiskFor(issue.changesets);
    if (worst) return ISSUE_BUCKET[worst.level] || ISSUE_BUCKET.unscored;
    if (issue.changesets && issue.changesets.length) return ISSUE_BUCKET.unscored;
    return ISSUE_BUCKET.no_commits;
}

function sortIssuesByRisk(issues) {
    return issues.slice().sort((a, b) => {
        const ba = issueBucket(a);
        const bb = issueBucket(b);
        if (ba !== bb) return bb - ba;
        return b.id - a.id;
    });
}

function renderChangeset(cs) {
    const li = document.createElement("li");
    li.className = "changeset";

    const meta = document.createElement("div");
    meta.className = "cs-meta";
    const rev = document.createElement("code");
    rev.textContent = cs.revision.length > 10 ? cs.revision.slice(0, 10) : cs.revision;
    meta.appendChild(rev);
    const who = document.createElement("span");
    who.className = "muted";
    who.textContent = " " + (cs.committer || "unknown") + " · " + (cs.committed_on || "");
    meta.appendChild(who);
    li.appendChild(meta);

    const msg = document.createElement("div");
    msg.className = "cs-msg";
    msg.textContent = (cs.message || "").split("\n")[0];
    li.appendChild(msg);

    li.appendChild(riskBadge(cs.risk));

    if (cs.diff_truncated) {
        const flag = document.createElement("span");
        flag.className = "muted small";
        flag.textContent = "(diff truncated)";
        li.appendChild(flag);
    }
    return li;
}

function renderIssueSummary(issue, asSummary) {
    const row = document.createElement(asSummary ? "summary" : "div");
    row.className = "issue-row issue-summary";

    const id = document.createElement("span");
    id.className = "col-id";
    id.textContent = "#" + issue.id;
    row.appendChild(id);

    const subj = document.createElement("span");
    subj.className = "col-subject";
    const subjText = document.createElement("span");
    subjText.className = "issue-subject";
    subjText.textContent = issue.subject || "";
    subj.appendChild(subjText);

    const n = (issue.changesets || []).length;
    const commits = document.createElement("span");
    commits.textContent = "Commits " + n;
    if (n > 0) {
        commits.className = "commits-toggle";
        commits.title = "Click to expand commit details";
    } else {
        commits.className = "commits-empty";
    }
    subj.appendChild(commits);
    row.appendChild(subj);

    const assigned = document.createElement("span");
    assigned.className = "col-assigned";
    if (issue.assigned_to) {
        assigned.textContent = initials(issue.assigned_to);
        assigned.title = issue.assigned_to;
    }
    row.appendChild(assigned);

    const priority = document.createElement("span");
    priority.className = "col-priority";
    priority.textContent = issue.priority || "";
    row.appendChild(priority);

    const status = document.createElement("span");
    status.className = "col-status";
    status.textContent = issue.status || "";
    row.appendChild(status);

    const riskCell = document.createElement("span");
    riskCell.className = "col-risk";
    const worst = worstRiskFor(issue.changesets);
    if (worst) {
        riskCell.appendChild(riskBadge(worst));
    } else if (n > 0) {
        riskCell.appendChild(riskBadge(null));
    } else {
        riskCell.textContent = "—";
    }
    row.appendChild(riskCell);

    return row;
}

function bindRowBehavior(row, issue, redmineBase, details) {
    const issueUrl = redmineBase.replace(/\/+$/, "") + "/issues/" + issue.id;
    const handler = (e) => {
        // Always suppress the default — we route the click ourselves.
        e.preventDefault();
        if (e.target.closest(".commits-toggle")) {
            if (details) details.open = !details.open;
            return;
        }
        // Anywhere else on the row: open the Redmine issue in a new tab.
        // Honor middle-click (auxclick fires for that path too).
        window.open(issueUrl, "_blank", "noopener,noreferrer");
    };
    row.addEventListener("click", handler);
    row.addEventListener("auxclick", (e) => {
        if (e.button === 1) handler(e);
    });
}

function renderIssue(issue, redmineBase) {
    const hasCommits = !!(issue.changesets && issue.changesets.length);
    if (!hasCommits) {
        const row = renderIssueSummary(issue, false);
        bindRowBehavior(row, issue, redmineBase, null);
        return row;
    }
    const details = document.createElement("details");
    details.className = "issue";
    const summary = renderIssueSummary(issue, true);
    details.appendChild(summary);
    bindRowBehavior(summary, issue, redmineBase, details);

    const ul = document.createElement("ul");
    ul.className = "changesets";
    for (const cs of issue.changesets) ul.appendChild(renderChangeset(cs));
    details.appendChild(ul);
    return details;
}

const CHART_COLORS = [
    "#4e79a7", "#f28e2b", "#59a14f", "#e15759",
    "#76b7b2", "#edc948", "#b07aa1", "#ff9da7",
];

function findTopRisks(issues, limit) {
    // Flatten all scored changesets, tag each with its parent issue, then
    // sort high → med → low. Stable tie-break on issue id desc.
    const all = [];
    for (const issue of issues) {
        for (const cs of issue.changesets || []) {
            if (cs.risk) all.push({ issue, cs });
        }
    }
    all.sort((a, b) => {
        const ra = RISK_RANK[a.cs.risk.level] || 0;
        const rb = RISK_RANK[b.cs.risk.level] || 0;
        if (ra !== rb) return rb - ra;
        return b.issue.id - a.issue.id;
    });
    return all.slice(0, limit);
}

function renderTopRisks(issues, redmineBase) {
    const wrap = document.getElementById("top-risks");
    const list = wrap.querySelector(".top-risks-list");
    const top = findTopRisks(issues, 6);
    if (!top.length) {
        wrap.hidden = true;
        return;
    }
    list.innerHTML = "";
    for (const { issue, cs } of top) {
        const li = document.createElement("li");
        li.className = "top-risk-item";

        li.appendChild(riskBadge(cs.risk));

        const ref = document.createElement("a");
        ref.className = "top-risk-ref";
        ref.href = redmineBase.replace(/\/+$/, "") + "/issues/" + issue.id;
        ref.target = "_blank";
        ref.rel = "noopener noreferrer";
        ref.textContent = "#" + issue.id;
        li.appendChild(ref);

        const reason = document.createElement("span");
        reason.className = "top-risk-reason";
        const reasons = cs.risk.reasons || [];
        reason.textContent = reasons[0] || (cs.message || "").split("\n")[0] || "(no reason given)";
        if (reasons.length > 1) {
            reason.title = reasons.join("\n");
        }
        li.appendChild(reason);

        list.appendChild(li);
    }
    wrap.hidden = false;
}

function renderStatusChart(issues) {
    const wrap = document.getElementById("status-chart-wrap");
    const canvas = document.getElementById("status-chart");
    const legend = document.getElementById("status-legend");

    const counts = {};
    for (const issue of issues) {
        const s = issue.status || "Unknown";
        counts[s] = (counts[s] || 0) + 1;
    }
    const entries = Object.entries(counts).sort((a, b) => b[1] - a[1]);
    if (!entries.length) { wrap.hidden = true; return; }

    const total = issues.length;
    const ctx = canvas.getContext("2d");
    const w = canvas.width;
    const cx = w / 2, cy = w / 2, r = w * 0.42;

    ctx.clearRect(0, 0, w, w);
    let angle = -Math.PI / 2;
    entries.forEach(([, count], i) => {
        const slice = (count / total) * 2 * Math.PI;
        ctx.beginPath();
        ctx.moveTo(cx, cy);
        ctx.arc(cx, cy, r, angle, angle + slice);
        ctx.closePath();
        ctx.fillStyle = CHART_COLORS[i % CHART_COLORS.length];
        ctx.fill();
        angle += slice;
    });

    legend.innerHTML = "";
    entries.forEach(([status, count], i) => {
        const item = document.createElement("div");
        item.className = "chart-legend-item";
        const swatch = document.createElement("span");
        swatch.className = "chart-swatch";
        swatch.style.background = CHART_COLORS[i % CHART_COLORS.length];
        item.appendChild(swatch);
        item.appendChild(document.createTextNode(status + " (" + count + ")"));
        legend.appendChild(item);
    });

    wrap.hidden = false;
}

async function loadVersion() {
    const name = versionFromUrl();
    if (!name) return;

    const title = document.getElementById("title");
    const status = document.getElementById("status");
    const notFound = document.getElementById("not-found");
    const issuesEl = document.getElementById("issues");
    const list = document.getElementById("issues-list");
    const lastFetched = document.getElementById("last-fetched");

    title.textContent = "Target version: " + name;

    try {
        const resp = await fetch("/api/v/" + encodeURIComponent(name));
        if (resp.status === 404) {
            notFound.hidden = false;
            issuesEl.hidden = true;
            document.getElementById("status-chart-wrap").hidden = true;
            document.getElementById("top-risks").hidden = true;
            status.textContent = "";
            return;
        }
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        const data = await resp.json();
        notFound.hidden = true;
        issuesEl.hidden = false;

        renderStatusChart(data.issues);
        const redmineBase = data.redmine_base || "";
        renderTopRisks(data.issues, redmineBase);

        list.innerHTML = "";
        const sorted = sortIssuesByRisk(data.issues);
        for (const issue of sorted) {
            list.appendChild(renderIssue(issue, redmineBase));
        }
        status.textContent = data.issues.length + " issues";
        if (data.last_fetched_at) {
            lastFetched.textContent = "Upstream last refreshed: " + data.last_fetched_at;
        } else {
            lastFetched.textContent = "Refreshing…";
        }
    } catch (e) {
        status.textContent = "Failed to load: " + e.message;
    }
}

loadVersion();
setInterval(loadVersion, 60000);
