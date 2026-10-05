"""README figures (`make figures`), drawn from the committed reports in `docs/reports/`.

Documentation tooling: it reads only the reports, never a split, a run file or the LLM, and the
service does not use it. Every number drawn is read from its report and checked to be written
there exactly as drawn, so a figure cannot show a value its report does not.

Writes an SVG (for the README) and a 300 dpi PNG of each figure to `docs/figures/`. Metadata is
fixed, so the same reports give the same files.
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.logging import configure_logging, get_logger

REPORTS = Path("docs/reports")
OUT = Path("docs/figures")

log = get_logger("pipeline.figures")

# Okabe-Ito: blue for TRAZO, orange for the free agent or a degraded cell, gray for references.
BLUE, ORANGE, GRAY = "#0072B2", "#E69F00", "#999999"
INK, INK2 = "#222222", "#666666"
TITLE, BODY, SMALL = 15, 11, 9

_PCT = r"(\d+\.\d+)%"
_IV = rf"{_PCT} \[{_PCT}, {_PCT}\] \((\d+/\d+)\)"


@dataclass(frozen=True)
class Estimate:
    """A rate with its 95% interval and its count, as written in a report."""

    value: str
    low: str
    high: str
    count: str


@dataclass(frozen=True)
class Figures:
    """The values of the four figures, each one checked against its report."""

    measures: list[tuple[str, Estimate, Estimate]]
    difference: tuple[str, str, str]
    coverage: list[tuple[str, Estimate]]
    service: Estimate
    variants: list[tuple[str, Estimate, str]]
    wilson: list[tuple[str, str, str]]


def section(text: str, heading: str) -> str:
    """Returns a report section, from its heading to the next heading of the same or higher level.

    Args:
        text: Report text.
        heading: The heading line, such as "## The five measures".

    Returns:
        The section text.

    Raises:
        ValueError: If the heading is not in the report.
    """
    start = text.find(heading + "\n")
    if start == -1:
        raise ValueError(f"heading not found: {heading}")
    level = heading.split(" ")[0]
    ends = [
        text.find(f"\n{h} ", start + len(heading))
        for h in ("#" * n for n in range(1, len(level) + 1))
    ]
    ends = [e for e in ends if e != -1]
    return text[start : min(ends) if ends else None]


def row(text: str, start: str) -> str:
    """Returns the table row of a section that starts with the given cell.

    Raises:
        ValueError: If no row starts that way.
    """
    for line in text.splitlines():
        if line.startswith(start):
            return line
    raise ValueError(f"row not found: {start}")


def written(text: str, *snippets: str) -> None:
    """Checks that each text to be drawn is written exactly so in the report section.

    Raises:
        ValueError: If one is not.
    """
    for s in snippets:
        if s not in text:
            raise ValueError(f"not written in the report: {s!r}")


def load(reports: Path = REPORTS) -> Figures:
    """Reads the values of the four figures from the reports and checks each one.

    Args:
        reports: Folder of the reports.

    Returns:
        The values, as strings exactly as the reports write them.

    Raises:
        ValueError: If a section, a row or a value is missing.
    """
    ev = (reports / "evaluacion.md").read_text(encoding="utf-8")
    an = (reports / "analisis.md").read_text(encoding="utf-8")

    five = section(ev, "## The five measures")
    measures = []
    for name in (
        "Safe automated resolution",
        "Containment",
        "Missed escalations",
        "Unnecessary escalations",
    ):
        found = re.findall(_IV, row(five, f"| {name} "))
        trazo, free = Estimate(*found[0]), Estimate(*found[1])
        written(five, f"{trazo.value}% [{trazo.low}%, {trazo.high}%] ({trazo.count})")
        written(five, f"{free.value}% [{free.low}%, {free.high}%] ({free.count})")
        measures.append((name, trazo, free))
    diff = re.search(
        r"([+-]\d+\.\d)% \[([+-]\d+\.\d)%, ([+-]\d+\.\d)%\]", row(five, "Paired difference")
    )
    if diff is None:
        raise ValueError("paired difference not found")
    written(five, f"{diff[1]}% [{diff[2]}%, {diff[3]}%]")

    ranker = section(an, "### Ranker against the manual score (CA3)")
    coverage = []
    for line in ranker.splitlines():
        m = re.match(
            rf"\| ([\d.]+) \| manual \| [\d.]+ \| (\d+/\d+) \({_PCT}; {_PCT} to {_PCT}\) \|", line
        )
        if m:
            alpha, count, value, low, high = m.groups()
            coverage.append((alpha, Estimate(value, low, high, count)))
            written(ranker, f"{count} ({value}%; {low}% to {high}%)")
    ref = re.search(rf"covered (\d+/\d+) \({_PCT}; {_PCT} to {_PCT}\)", ranker)
    if not coverage or ref is None:
        raise ValueError("coverage rows not found")
    service = Estimate(ref[2], ref[3], ref[4], ref[1])

    sens = section(ev, "## Sensitivity of the thresholds")
    variants = []
    for name in ("amounts_half", "base", "alpha_0.10", "amounts_double"):
        cells = row(sens, f"| {name} |").split(" | ")
        contained = Estimate(*re.findall(_IV, cells[2])[0])
        unsafe = cells[4].split("/")[0]
        written(
            sens, f"{contained.value}% [{contained.low}%, {contained.high}%] ({contained.count})"
        )
        written(sens, f"| {unsafe}/376 |")
        variants.append((name, contained, unsafe))

    known = section(ev, "### Known error rates [simulado]")
    wilson = []
    for rate in ("10%", "40%"):
        m = re.search(r"\(" + _PCT + r"\).*?\(" + _PCT + r"\)", row(known, f"| {rate} |"))
        if m is None:
            raise ValueError(f"Wilson row not found: {rate}")
        written(known, f"({m[1]}%)", f"({m[2]}%)")
        wilson.append((rate, m[1], m[2]))

    return Figures(measures, (diff[1], diff[2], diff[3]), coverage, service, variants, wilson)


def _frame(plt: Any, title: str, subtitle: str, tag: str, source: str) -> Any:
    fig = plt.figure(figsize=(10, 5.625))
    fig.text(0.05, 0.93, title, fontsize=TITLE, fontweight="bold", color=INK, va="bottom")
    fig.text(0.05, 0.875, subtitle, fontsize=BODY, color=INK2, va="bottom")
    fig.text(0.95, 0.94, tag, fontsize=BODY, color=INK2, ha="right", va="bottom")
    fig.text(0.05, 0.03, f"Source: {source}", fontsize=SMALL, color=INK2, va="bottom")
    return fig


def _clean(ax: Any, left: bool = True) -> None:
    for side in ("top", "right") if left else ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRAY)
    if left:
        ax.spines["left"].set_color(GRAY)
    else:
        ax.set_yticks([])
    ax.tick_params(colors=INK2, labelsize=BODY, length=0)


def _save(fig: Any, name: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT / f"{name}.svg", metadata={"Date": None, "Creator": None})
    fig.savefig(OUT / f"{name}.png", dpi=300, metadata={"Software": None})
    log.info("figure_written", name=name)


def draw(f: Figures) -> None:
    """Draws the four figures into `docs/figures/`.

    Args:
        f: The checked values from `load`.
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": BODY,
            "figure.facecolor": "white",
            "axes.facecolor": "white",
            "svg.hashsalt": "trazo-figures",
        }
    )

    # Measures against the free agent
    fig = _frame(
        plt,
        "TRAZO against a free agent",
        f"376 held-out cases. Safe automated resolution: {f.difference[0]} points "
        f"[{f.difference[1]}, {f.difference[2]}].",
        "Measured offline",
        "docs/reports/evaluacion.md, The five measures",
    )
    ax = fig.add_axes((0.05, 0.2, 0.9, 0.6))
    width = 0.36
    names = {
        "Safe automated resolution": "Safe automated\nresolution",
        "Containment": "Containment",
        "Missed escalations": "Missed escalations\n(lower is better)",
        "Unnecessary escalations": "Unnecessary escalations\n(lower is better)",
    }
    for i, (_name, trazo, free) in enumerate(f.measures):
        for dx, est, color, who in (
            (-width / 2, trazo, BLUE, "TRAZO"),
            (width / 2, free, ORANGE, "Free agent"),
        ):
            v, lo, hi = float(est.value), float(est.low), float(est.high)
            ax.bar(i + dx, v, width - 0.03, color=color)
            if hi > lo:
                ax.errorbar(
                    i + dx,
                    v,
                    yerr=[[v - lo], [hi - v]],
                    fmt="none",
                    ecolor=INK,
                    elinewidth=1,
                    capsize=4,
                )
            label = f"{who}\n{est.value}%" if i == 0 else f"{est.value}%"
            ax.text(
                i + dx, max(v, hi) + 2, label, ha="center", va="bottom", fontsize=BODY, color=INK
            )
    ax.set_xticks(range(len(f.measures)), [names[m[0]] for m in f.measures])
    ax.set_ylim(0, 105)
    _clean(ax, left=False)
    _save(fig, "trazo-vs-free-agent")
    plt.close(fig)

    # Conformal coverage against the target
    fig = _frame(
        plt,
        "Conformal coverage against the target",
        "Share of the 304 held-out cases whose true charge is in the candidate set.",
        "Measured offline",
        "docs/reports/analisis.md, Ranker against the manual score",
    )
    ax = fig.add_axes((0.09, 0.2, 0.86, 0.6))
    targets = [100 * (1 - float(a)) for a, _ in f.coverage]
    values = [float(e.value) for _, e in f.coverage]
    ax.plot([78, 100], [78, 100], ls="--", color=GRAY, lw=1.2)
    ax.text(83, 80.8, "target", color=INK2, fontsize=BODY, ha="left", va="top")
    ax.plot(targets, values, color=BLUE, lw=2)
    ax.errorbar(
        targets,
        values,
        yerr=[
            [v - float(e.low) for v, (_, e) in zip(values, f.coverage, strict=True)],
            [float(e.high) - v for v, (_, e) in zip(values, f.coverage, strict=True)],
        ],
        fmt="o",
        ms=6,
        color=BLUE,
        elinewidth=1,
        capsize=4,
    )
    for t, v, (_, e) in zip(targets, values, f.coverage, strict=True):
        ax.text(t - 0.5, v + 0.8, f"{e.value}%", ha="right", va="bottom", fontsize=BODY, color=INK)
    s = float(f.service.value)
    # Drawn just right of the curve point at the same 95% target, so the two stay apart.
    ax.errorbar(
        [95.45],
        [s],
        yerr=[[s - float(f.service.low)], [float(f.service.high) - s]],
        fmt="D",
        ms=8,
        color=BLUE,
        mfc="white",
        mew=1.8,
        elinewidth=1,
        capsize=4,
    )
    ax.text(
        95.9, s - 3.4, f"service: {f.service.value}%", ha="left", va="top", fontsize=BODY, color=INK
    )
    ax.set_xlim(78, 100)
    ax.set_ylim(74, 103)
    ax.set_xticks(targets, [f"{t:g}%" for t in targets])
    ax.set_yticks([75, 80, 85, 90, 95, 100], [f"{y}%" for y in (75, 80, 85, 90, 95, 100)])
    ax.set_xlabel("Target coverage (1 - alpha)", fontsize=BODY, color=INK2)
    _clean(ax)
    _save(fig, "conformal-coverage")
    plt.close(fig)

    # Autonomy against unsafe outcomes
    fig = _frame(
        plt,
        "Autonomy against unsafe outcomes",
        "Approval thresholds and alpha varied on the 376 held-out cases.",
        "Measured offline",
        "docs/reports/evaluacion.md, Sensitivity of the thresholds",
    )
    ax = fig.add_axes((0.09, 0.2, 0.86, 0.6))
    xs = [int(u) for _, _, u in f.variants]
    ys = [float(e.value) for _, e, _ in f.variants]
    ax.plot(xs, ys, color=BLUE, lw=2)
    ax.errorbar(
        xs,
        ys,
        yerr=[
            [y - float(e.low) for y, (_, e, _) in zip(ys, f.variants, strict=True)],
            [float(e.high) - y for y, (_, e, _) in zip(ys, f.variants, strict=True)],
        ],
        fmt="o",
        ms=6,
        color=BLUE,
        elinewidth=1,
        capsize=4,
    )
    labels = {
        "amounts_half": ("Amounts halved", 1.0, -6, "left"),
        "base": ("Chosen: 500 / 1,000 USD, alpha 0.05", -1.0, 9, "right"),
        "alpha_0.10": ("alpha 0.10", 0.8, -9, "left"),
        "amounts_double": ("Amounts doubled", -0.8, 9, "right"),
    }
    for (name, e, u), x, y in zip(f.variants, xs, ys, strict=True):
        text, dx, dy, ha = labels[name]
        weight = "bold" if name == "base" else "normal"
        ax.text(
            x + dx,
            y + dy,
            f"{text}\n{e.value}%, {u} unsafe",
            ha=ha,
            va="center",
            fontsize=BODY,
            color=INK,
            fontweight=weight,
        )
    base = (
        xs[[n for n, _, _ in f.variants].index("base")],
        ys[[n for n, _, _ in f.variants].index("base")],
    )
    ax.plot(*base, marker="o", ms=13, mfc="none", mec=INK, mew=1.5)
    ax.set_xlim(14, 54)
    ax.set_ylim(25, 100)
    ax.set_yticks([25, 50, 75, 100], ["25%", "50%", "75%", "100%"])
    ax.set_xlabel("Cases with any unsafe outcome, of 376", fontsize=BODY, color=INK2)
    ax.set_ylabel("Containment", fontsize=BODY, color=INK2)
    _clean(ax)
    _save(fig, "autonomy-vs-unsafe")
    plt.close(fig)

    # Wilson detection of a known error rate
    fig = _frame(
        plt,
        "How often Wilson demotes a cell",
        "10,000 simulated review streams per true error rate; blocks of 20 reviews.",
        "Simulation",
        "docs/reports/evaluacion.md, Known error rates",
    )
    ax = fig.add_axes((0.05, 0.2, 0.9, 0.6))
    groups = {
        "10%": ("10% true error (healthy cell)", BLUE),
        "40%": ("40% true error (degraded cell)", ORANGE),
    }
    ticks, tick_labels = [], []
    for i, (rate, first, within) in enumerate(f.wilson):
        title, color = groups[rate]
        for dx, value, when in ((-0.2, first, "1st block"), (0.2, within, "within 10 blocks")):
            ax.bar(i + dx, float(value), 0.37, color=color)
            ax.text(
                i + dx,
                float(value) + 2,
                f"{value}%",
                ha="center",
                va="bottom",
                fontsize=BODY,
                color=INK,
            )
            ticks.append(i + dx)
            tick_labels.append(when)
        ax.text(i, -11, title, ha="center", va="top", fontsize=BODY, color=INK)
    ax.set_xticks(ticks, tick_labels)
    ax.set_ylim(0, 105)
    _clean(ax, left=False)
    _save(fig, "wilson-detection")
    plt.close(fig)


def main() -> None:
    """Checks the values against the reports and writes the four figures."""
    configure_logging("INFO")
    draw(load())


if __name__ == "__main__":
    main()
