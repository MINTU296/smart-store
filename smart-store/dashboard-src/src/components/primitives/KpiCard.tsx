import React from 'react';

interface Props {
    label: string;
    value: React.ReactNode;
    sub?: React.ReactNode;
    delta?: React.ReactNode;
    hero?: boolean;
    pastel?: boolean;
}

export const KpiCard: React.FC<Props> = ({ label, value, sub, delta, hero, pastel }) => {
    const cls = ['card', hero && 'hero', pastel && 'pastel'].filter(Boolean).join(' ');
    return (
        <div className={cls}>
            <div className="card-label">{label}</div>
            <div className="card-value">{value}</div>
            {sub && <div className="card-sub">{sub}</div>}
            {delta}
        </div>
    );
};
