"""
Temperature converter — toy project for agent sandbox testing.

Usage:
    python3 app.py 100 C F      # 100°C → 212.0°F
    python3 app.py 72  F C      # 72°F  → 22.22°C
    python3 app.py 300 K C      # 300K  → 26.85°C
"""

import sys


def convert(value: float, from_unit: str, to_unit: str) -> float:
    """Convert between Celsius (C), Fahrenheit (F), and Kelvin (K)."""
    # Normalise to Celsius first
    if from_unit == "C":
        celsius = value
    elif from_unit == "F":
        celsius = (value - 32) * 5 / 9
    elif from_unit == "K":
        celsius = value - 273.15
    else:
        raise ValueError(f"Unknown unit: {from_unit}")

    # Convert from Celsius to target
    if to_unit == "C":
        return celsius
    elif to_unit == "F":
        return celsius * 9 / 5 + 32
    elif to_unit == "K":
        return celsius + 273.15
    else:
        raise ValueError(f"Unknown unit: {to_unit}")


def main() -> None:
    if len(sys.argv) != 4:
        print(__doc__.strip())
        sys.exit(1)

    value = float(sys.argv[1])
    from_unit = sys.argv[2].upper()
    to_unit = sys.argv[3].upper()

    result = convert(value, from_unit, to_unit)
    print(f"{value}°{from_unit} = {result:.2f}°{to_unit}")


if __name__ == "__main__":
    main()
