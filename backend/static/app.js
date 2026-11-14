"use strict";
const $ = (id) => document.getElementById(id);
const files = { report: null, summary: null };
const previews = {};
let stream, deadline, currentReview, submission, busy = false;
function message(text, error = false) {
  $("message").textContent = text;
  $("message").className = error ? "error" : "";
  $("message").hidden = !text;
}
function setBusy(value) {
  busy = value;
  document.body.classList.toggle("busy", value);
  $("compare").disabled = value || !files.report || !files.summary;
  for (const kind of ["report", "summary"]) $(kind).disabled = value;
}
function choose(kind, file) {
  submission = null;
  $("results").hidden = true;
  $("reconnect").hidden = true;
  $("failure-evidence").hidden = true;
  if (previews[kind]) URL.revokeObjectURL(previews[kind]);
  if (file && (!/\.pdf$/i.test(file.name) || file.size > 5 * 1024 * 1024 || !file.size)) {
    files[kind] = null;
    $(kind).value = "";
    $(kind + "-name").textContent = "Choose a non-empty PDF up to 5 MiB.";
    $(kind + "-view").hidden = true;
    message("Please choose a non-empty, text-based PDF up to 5 MiB.", true);
  } else {
    files[kind] = file;
    $(kind + "-name").textContent = file ? `${file.name} · ${(file.size / 1024).toFixed(1)} KiB` : "No document selected";
    if (file) {
      const selection = new DataTransfer();
      selection.items.add(file);
      $(kind).files = selection.files;
      previews[kind] = URL.createObjectURL(file);
      $(kind + "-view").href = previews[kind];
    }
    $(kind + "-view").hidden = !file;
  }
  setBusy(busy);
}
async function api(path, options) {
  const response = await fetch(path, options);
  let data;
  try { data = await response.json(); }
  catch { throw new Error("The service did not return a review response. Reload this page and try again."); }
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "The request could not be completed. Check your documents and try again.");
  return data;
}
function stopStream() {
  if (stream) stream.close();
  clearTimeout(deadline);
}
function render(review) {
  const findings = review.findings || [];
  $("findings").replaceChildren();
  for (const finding of findings) {
    const card = $("finding-template").content.cloneNode(true);
    const label = { issue: "Difference found", consistent: "Consistent", unresolved: "Needs review" }[finding.classification] || "Needs review";
    card.querySelector("h3").textContent = finding.check === "narrative" ? "Narrative comparison" : finding.metric ? `${finding.metric.replaceAll("_", " ")} · ${finding.check}` : `${finding.check} check`;
    const badge = card.querySelector(".badge");
    badge.textContent = label;
    badge.classList.add(["consistent", "unresolved"].includes(finding.classification) ? finding.classification : "issue");
    card.querySelector(".explanation").textContent = finding.explanation || "Review the cited figures and their context.";
    const quotes = card.querySelectorAll("blockquote");
    ["report", "summary"].forEach((kind, index) => {
      const evidence = finding[kind + "_evidence"] ? [finding[kind + "_evidence"]] : finding.source === kind ? finding.evidence || [] : [];
      if (finding.source && finding.source !== kind) {
        quotes[index].hidden = true;
        card.querySelector(".quotes").classList.add("single");
      }
      const pages = [...new Set(evidence.map((quote) => quote.page))];
      quotes[index].querySelector(".page").textContent = pages.length ? `${pages.length === 1 ? "Page" : "Pages"} ${pages.join(", ")}` : "";
      quotes[index].querySelector("p").textContent = evidence.length ? evidence.map((quote) => `“${quote.quote}”`).join("\n\n") : "No paired quote was established for this check.";
    });
    $("findings").append(card);
  }
  const issues = findings.filter((f) => f.classification === "issue").length;
  const unresolved = findings.filter((f) => f.classification === "unresolved").length;
  $("summary-line").textContent = `${findings.length} checks · ${issues} ${issues === 1 ? "difference" : "differences"} found${unresolved ? ` · ${unresolved} need review` : ""}`;
  $("review-reference").textContent = `Review ${review.id}`;
  $("evidence-link").href = `/api/reviews/${review.id}/evidence`;
  $("results").hidden = false;
  $("results").scrollIntoView({ behavior: "smooth", block: "start" });
}
function follow(id) {
  stopStream();
  currentReview = id;
  $("reconnect").hidden = true;
  setBusy(true);
  stream = new EventSource(`/api/reviews/${id}/events`);
  stream.onmessage = ({ data }) => {
    const review = JSON.parse(data);
    if (review.status === "completed") {
      stopStream(); setBusy(false); message("Comparison complete. Review the findings below."); render(review);
    } else if (review.status === "failed") {
      stopStream(); setBusy(false); message("This comparison could not be completed. Your review has been saved; check the processing evidence or try a new comparison.", true);
      $("failure-evidence").href = `/api/reviews/${id}/evidence`;
      $("failure-evidence").hidden = false;
      submission = null;
    } else message(review.status === "running" ? "Comparing the report and summary. Findings will appear here automatically." : "Your review is queued. You can keep this page open while it is processed.");
  };
  stream.onerror = () => message("Reconnecting to your review. Processing continues on the server.");
  deadline = setTimeout(() => {
    stopStream(); setBusy(false); message("The review is taking longer than expected. Processing continues on the server."); $("reconnect").hidden = false;
  }, 300000);
}
for (const kind of ["report", "summary"]) $(kind).addEventListener("change", () => choose(kind, $(kind).files[0] || null));
$("example").addEventListener("click", async () => {
  try {
    message("Loading the sample documents…");
    for (const kind of ["report", "summary"]) {
      const response = await fetch(`/static/examples/${kind}.pdf`);
      if (!response.ok) throw new Error("Sample documents are unavailable. You can still choose your own PDFs.");
      choose(kind, new File([await response.blob()], `Sample ${kind}.pdf`, { type: "application/pdf" }));
    }
    message("Samples loaded: report revenue £100 and declining margin; summary revenue £110 and improving margin. Select Compare documents to review them.");
  } catch (error) { message(error.message, true); }
});
$("comparison").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (busy || !files.report || !files.summary) return;
  setBusy(true); stopStream(); $("results").hidden = true; $("reconnect").hidden = true;
  try {
    submission ||= { report: crypto.randomUUID(), summary: crypto.randomUUID(), key: crypto.randomUUID() };
    const versions = {};
    for (const kind of ["report", "summary"]) {
      message(`Uploading and verifying the ${kind}…`);
      versions[kind] = await api(`/api/documents/${kind}/${submission[kind]}`, { method: "PUT", headers: { "Content-Type": "application/pdf" }, body: files[kind] });
    }
    const review = await api("/api/reviews", { method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": submission.key }, body: JSON.stringify({ report_version_id: versions.report.id, summary_version_id: versions.summary.id, check_profile_version: "financial-v1" }) });
    history.replaceState(null, "", `#review=${review.id}`);
    follow(review.id);
  } catch (error) { setBusy(false); message(error.message, true); }
});
$("reconnect").addEventListener("click", () => follow(currentReview));
window.addEventListener("pagehide", stopStream);
const saved = location.hash.match(/^#review=([0-9a-f-]{36})$/i);
if (saved) follow(saved[1]);
