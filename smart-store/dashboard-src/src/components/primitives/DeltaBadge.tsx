import React from 'react';

interface Props {
    value: number;        // already in pp or pct
    unit: 'pp' | '%';
    /** When delta is negative, this metric is "good when going down"  */
    inverse?: boolean;
}

export const DeltaBadge: React.FC<Props> = ({ value, unit, inverse = false }) => {
    if (value === null || value === undefined || Number.isNaN(value)) return null;
    const direction = value > 0 ? 'up' : value < 0 ? 'down' : 'flat';
    const goodish =
        direction === 'flat'
            ? 'flat'
            : inverse
                ? (direction === 'down' ? 'up' : 'down')   // inverted: down = good
                : direction;
    const arrow = direction === 'up' ? '▲' : direction === 'down' ? '▼' : '·';
    const sign = value > 0 ? '+' : '';
    const display = `${sign}${value.toFixed(1)} ${unit}`;
    return (
        <div className={`delta ${goodish}`}>
            <span>{arrow}</span>
            <span>{display} vs 7d</span>
        </div>
    );
};
