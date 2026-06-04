import React, { useState } from 'react';
import './theme.css';

import { TopBar } from './components/TopBar';
import { HeadlineMetrics } from './components/HeadlineMetrics';
import { ZoneHeatmap } from './components/ZoneHeatmap';
import { ConversionFunnel } from './components/ConversionFunnel';
import { AnomalyFeed } from './components/AnomalyFeed';
import { TrafficByHour } from './components/TrafficByHour';
import { AttentionVsConversion } from './components/AttentionVsConversion';
import { StaffVsCustomers } from './components/StaffVsCustomers';
import { SessionChips } from './components/SessionChips';
import { LiveEventStream } from './components/LiveEventStream';
import { useStoreIntelligence } from './hooks/useStoreIntelligence';

const App: React.FC = () => {
    const [target, setTarget] = useState('STORE_BLR_001');
    useStoreIntelligence(target);

    return (
        <div className="shell">
            <TopBar onSwitch={setTarget} />

            <HeadlineMetrics />

            <div className="section-title">Where attention goes & where it leaks</div>
            <div className="grid row-three">
                <ZoneHeatmap />
                <ConversionFunnel />
                <AnomalyFeed />
            </div>

            <div className="section-title">Deeper retail signals</div>
            <div className="grid row-two">
                <TrafficByHour />
                <AttentionVsConversion />
            </div>

            <div className="grid row-two" style={{ marginTop: 16 }}>
                <StaffVsCustomers />
                <SessionChips />
            </div>

            <div className="section-title">Live event stream</div>
            <LiveEventStream />
        </div>
    );
};

export default App;
