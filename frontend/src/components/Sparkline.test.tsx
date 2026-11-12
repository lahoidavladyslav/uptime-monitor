import { render, screen } from "@testing-library/react";

import { Sparkline } from "./Sparkline";

describe("Sparkline", () => {
  it("draws separate sub-paths around gaps (null)", () => {
    const { container } = render(<Sparkline values={[1, 2, null, 3, 4]} />);
    const d = container.querySelector("path")?.getAttribute("d") ?? "";
    expect(d.match(/M/g)).toHaveLength(2);
    expect(d).not.toMatch(/NaN|undefined/);
  });

  it("all-null shows the 'no data' placeholder, never a broken chart", () => {
    const { container } = render(<Sparkline values={[null, null, null]} />);
    expect(screen.getByTestId("sparkline-empty")).toBeInTheDocument();
    expect(screen.getByLabelText("немає даних")).toBeInTheDocument();
    expect(container.innerHTML).not.toMatch(/NaN/);
  });

  it("empty input is treated like no data", () => {
    render(<Sparkline values={[]} />);
    expect(screen.getByTestId("sparkline-empty")).toBeInTheDocument();
  });

  it("a flat series (max == min) does not divide by zero", () => {
    const { container } = render(<Sparkline values={[5, 5, 5, 5]} />);
    expect(container.innerHTML).not.toMatch(/NaN|Infinity/);
    expect(container.querySelector("path")).not.toBeNull();
  });

  it("a single point still renders something visible", () => {
    const { container } = render(<Sparkline values={[null, 7, null]} />);
    expect(container.querySelector("path")?.getAttribute("d")).toMatch(/^M/);
    expect(container.innerHTML).not.toMatch(/NaN/);
  });
});
