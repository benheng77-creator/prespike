import numpy as np

class QuantumSignalFusion:
    def __init__(self):
        # Weights for HFT, StatArb, OrderFlow, On-Chain, Sentiment
        self.weights = np.array([0.3, 0.25, 0.2, 0.15, 0.1])

    def evaluate_regime(self, data_vector):
        """
        data_vector: array of normalized alpha signals from -1 (short) to 1 (long)
        Returns unified signal and probability
        """
        fused_score = np.dot(data_vector, self.weights)
        probability = 1 / (1 + np.exp(-fused_score)) # Sigmoid activation

        signal = {
            'action': 'BUY' if fused_score > 0.6 else 'SELL' if fused_score < -0.6 else 'HOLD',
            'win_prob': probability,
            'reward_risk': 3.5 # Minimum asymmetric target
        }
        return signal
