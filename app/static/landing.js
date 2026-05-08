// "Release X.Y" exactly: matches the named-release base versions.
const FEATURED_RE = /^Release (\d+)\.(\d+)$/;
// "Release X" or "Release X.Y" or "Release X.Y.Z": anything we can group.
const RELEASE_RE = /^Release (\d+)(?:\.(\d+))?(?:\.(\d+))?(?:\.(\d+))?$/;
const MISC_GROUP = "Miscellaneous";

function parseFeatured(name) {
    const m = name && name.match(FEATURED_RE);
    if (!m) return null;
    return [parseInt(m[1], 10), parseInt(m[2], 10)];
}

function parseRelease(name) {
    const m = name && name.match(RELEASE_RE);
    if (!m) return null;
    return [1, 2, 3, 4].map((i) => parseInt(m[i] || "0", 10));
}

function compare(a, b) {
    for (let i = 0; i < a.length; i++) {
        if (a[i] !== b[i]) return a[i] - b[i];
    }
    return 0;
}

function groupKey(name) {
    const parsed = parseRelease(name);
    if (!parsed) return MISC_GROUP;
    return parsed[0] + ".x";
}

function findFeatured(versions) {
    let current = null;
    let previous = null;
    for (const v of versions) {
        const parsed = parseFeatured(v.name);
        if (!parsed) continue;
        const tagged = { ...v, _parsed: parsed };
        if (v.status === "open") {
            if (!current || compare(tagged._parsed, current._parsed) > 0) {
                current = tagged;
            }
        }
    }
    for (const v of versions) {
        const parsed = parseFeatured(v.name);
        if (!parsed) continue;
        if (v.status !== "closed") continue;
        if (current && compare(parsed, current._parsed) >= 0) continue;
        if (!previous || compare(parsed, previous._parsed) > 0) {
            previous = { ...v, _parsed: parsed };
        }
    }
    return { current, previous };
}

function renderFeaturedCard(el, version) {
    if (!version) {
        el.hidden = true;
        return;
    }
    el.hidden = false;
    const link = el.querySelector(".featured-name");
    link.textContent = version.name;
    link.href = "/v/" + encodeURIComponent(version.name);
    const meta = el.querySelector(".featured-meta");
    meta.innerHTML = "";
    const bits = [version.status];
    if (version.due_date) bits.push("due " + version.due_date);
    meta.appendChild(document.createTextNode(bits.join(" · ")));
    if (version.issue_count) {
        meta.appendChild(document.createTextNode(" · "));
        const badge = document.createElement("span");
        badge.className = "issue-count-badge";
        badge.textContent = version.issue_count + " issues";
        meta.appendChild(badge);
    }

    const review = el.querySelector(".featured-review");
    if (review) {
        if (version.total_changesets > 0) {
            review.textContent = version.scored_changesets + " of " + version.total_changesets + " commits reviewed";
        } else {
            review.textContent = "";
        }
    }
}

function findPatchQueue(versions) {
    return versions.find(v => v.name === "Patch Queue") || null;
}

async function renderPatchQueue(versions) {
    const section = document.getElementById("patch-queue-section");
    const patchQueue = findPatchQueue(versions);
    if (!patchQueue) { section.hidden = true; return; }

    section.querySelector(".patch-queue-name").textContent = patchQueue.name;
    section.querySelector(".patch-queue-name").href = "/v/" + encodeURIComponent(patchQueue.name);
    const bits = [patchQueue.status];
    if (patchQueue.due_date) bits.push("due " + patchQueue.due_date);
    section.querySelector(".patch-queue-meta").textContent = bits.join(" · ");
    section.hidden = false;

    try {
        const resp = await fetch("/api/v/" + encodeURIComponent(patchQueue.name));
        if (!resp.ok) return;
        const data = await resp.json();
        const rb = (data.redmine_base || "").replace(/\/+$/, "");

        const countEl = section.querySelector(".patch-queue-count");
        countEl.textContent = data.issues.length + " issues";
        countEl.className = "patch-queue-count " + (data.issues.length > 0 ? "issue-count-red" : "issue-count-badge");
        section.classList.toggle("has-issues", data.issues.length > 0);

        const list = section.querySelector(".patch-queue-issues");
        list.innerHTML = "";
        for (const issue of data.issues) {
            const li = document.createElement("li");
            li.className = "pq-issue";

            const ref = document.createElement("a");
            ref.className = "pq-ref";
            ref.href = rb + "/issues/" + issue.id;
            ref.target = "_blank";
            ref.rel = "noopener noreferrer";
            ref.textContent = "#" + issue.id;
            li.appendChild(ref);

            const subj = document.createElement("span");
            subj.className = "pq-subject";
            subj.textContent = issue.subject || "";
            li.appendChild(subj);

            const status = document.createElement("span");
            status.className = "pq-status";
            status.textContent = issue.status || "";
            li.appendChild(status);

            list.appendChild(li);
        }
    } catch (_) { /* silently ignore */ }
}

function buildGroups(versions) {
    const groups = new Map();
    for (const v of versions) {
        const key = groupKey(v.name);
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(v);
    }
    // Sort entries within each group.
    for (const [key, items] of groups) {
        if (key === MISC_GROUP) {
            items.sort((a, b) => a.name.localeCompare(b.name));
        } else {
            items.sort((a, b) => {
                const pa = parseRelease(a.name) || [0, 0, 0, 0];
                const pb = parseRelease(b.name) || [0, 0, 0, 0];
                return compare(pb, pa); // descending
            });
        }
    }
    // Sort group keys: numeric majors desc, Misc last.
    const sortedKeys = [...groups.keys()].sort((a, b) => {
        if (a === MISC_GROUP) return 1;
        if (b === MISC_GROUP) return -1;
        return parseInt(b, 10) - parseInt(a, 10);
    });
    return sortedKeys.map((key) => ({ key, items: groups.get(key) }));
}

function renderGroups(container, groups) {
    container.innerHTML = "";
    for (const { key, items } of groups) {
        const details = document.createElement("details");
        details.className = "version-group";

        const summary = document.createElement("summary");
        summary.className = "version-group-summary";
        const title = document.createElement("span");
        title.className = "version-group-title";
        title.textContent = key === MISC_GROUP ? key : `${key} releases`;
        const count = document.createElement("span");
        count.className = "version-group-count muted";
        count.textContent = items.length.toString();
        summary.appendChild(title);
        summary.appendChild(count);
        details.appendChild(summary);

        const ul = document.createElement("ul");
        ul.className = "versions";
        for (const v of items) {
            const li = document.createElement("li");
            const a = document.createElement("a");
            a.href = "/v/" + encodeURIComponent(v.name);
            a.textContent = v.name;
            li.appendChild(a);
            const meta = document.createElement("span");
            meta.className = "muted";
            meta.textContent =
                " — " +
                (v.status || "unknown") +
                (v.due_date ? " (due " + v.due_date + ")" : "");
            li.appendChild(meta);
            ul.appendChild(li);
        }
        details.appendChild(ul);
        container.appendChild(details);
    }
}

async function loadVersions() {
    const status = document.getElementById("status");
    const groupsContainer = document.getElementById("version-groups");
    const lastFetched = document.getElementById("last-fetched");
    const featured = document.getElementById("featured");
    const allHeading = document.getElementById("all-heading");

    try {
        const resp = await fetch("/api/versions");
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        const data = await resp.json();

        if (!data.versions.length) {
            status.textContent = "No versions found in Redmine yet.";
            featured.hidden = true;
            allHeading.hidden = true;
            groupsContainer.innerHTML = "";
            return;
        }
        status.textContent = "";

        const { current, previous } = findFeatured(data.versions);
        renderFeaturedCard(document.getElementById("featured-current"), current);
        renderFeaturedCard(document.getElementById("featured-previous"), previous);
        featured.hidden = !(current || previous);
        allHeading.hidden = false;

        await renderPatchQueue(data.versions);
        renderGroups(groupsContainer, buildGroups(data.versions));

        if (data.last_fetched_at) {
            lastFetched.textContent = "Versions last refreshed: " + data.last_fetched_at;
        }
    } catch (e) {
        status.textContent = "Failed to load versions: " + e.message;
    }
}

loadVersions();
setInterval(loadVersions, 60000);
