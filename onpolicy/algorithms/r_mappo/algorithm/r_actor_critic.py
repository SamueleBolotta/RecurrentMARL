import torch
import torch.nn as nn
import numpy as np
from onpolicy.algorithms.utils.util import init, check
from onpolicy.algorithms.utils.cnn import CNNBase
from onpolicy.algorithms.utils.mlp import MLPBase
from onpolicy.algorithms.utils.rnn import RNNLayer
from onpolicy.algorithms.utils.multiwalker_act import ACTLayer as multiwalker_act
from onpolicy.algorithms.utils.mpe_act import ACTLayer as mpe_act
from onpolicy.algorithms.utils.popart import PopArt
from onpolicy.utils.util import get_shape_from_obs_space
from onpolicy.algorithms.utils.RIM import RIMCell

class R_Actor(nn.Module):
    """
    Actor network class for MAPPO. Outputs actions given observations.
    :param args: (argparse.Namespace) arguments containing relevant model information.
    :param obs_space: (gym.Space) observation space.
    :param action_space: (gym.Space) action space.
    :param device: (torch.device) specifies the device to run on (cpu/gpu).
    """
    def __init__(self, args, obs_space, action_space, device=torch.device("cpu")):
        super(R_Actor, self).__init__()
        self.hidden_size = args.hidden_size
        self._env_name = args.env_name
        print("environment: ", self._env_name)
        self._gain = args.gain
        self._use_orthogonal = args.use_orthogonal
        self._use_policy_active_masks = args.use_policy_active_masks
        self._use_naive_recurrent_policy = args.use_naive_recurrent_policy
        self._use_recurrent_policy = args.use_recurrent_policy
        self._use_rims_policy_LSTM = args.use_rims_policy_LSTM
        self._use_rims_policy_GRU = args.use_rims_policy_GRU
        self._use_lstm_policy = args.use_lstm_policy
        self._recurrent_N = args.recurrent_N
        self._num_units = args.num_units
        self.tpdv = dict(dtype=torch.float32, device=device)

        obs_shape = get_shape_from_obs_space(obs_space)
                
        base1 = nn.Sequential(
            self._layer_init(nn.Conv2d(4, 32, 3, padding=1)),
            nn.MaxPool2d(2),
            nn.ReLU(),
            self._layer_init(nn.Conv2d(32, 64, 3, padding=1)),
            nn.MaxPool2d(2),
            nn.ReLU(),
            self._layer_init(nn.Conv2d(64, 128, 3, padding=1)),
            nn.MaxPool2d(2),
            nn.ReLU(),
            nn.Flatten(),
            self._layer_init(nn.Linear(128 * 8 * 8, 512)),
            nn.ReLU(),
            self._layer_init(nn.Linear(512, 64)),
            nn.ReLU(),
        )
        
        base2 = MLPBase
        
        self.base = base1 if len(obs_shape) == 3 else base2(args, obs_shape)        
        if self._use_naive_recurrent_policy or self._use_recurrent_policy:
            if self._use_rims_policy_LSTM:  
                self.rnn = RIMCell(torch.device('cuda' if torch.cuda.is_available() else 'cpu'), self.hidden_size, self.hidden_size // self._num_units, self._num_units, 1, 'LSTM', input_value_size = 64, comm_value_size = self.hidden_size // self._num_units)
            elif self._use_rims_policy_GRU:  
                self.rnn = RIMCell(torch.device('cuda' if torch.cuda.is_available() else 'cpu'), self.hidden_size, self.hidden_size // self._num_units, self._num_units, 1, 'GRU', input_value_size = 64, comm_value_size = self.hidden_size // self._num_units)
            elif self._use_lstm_policy:
                self.rnn = RNNLayer(self.hidden_size, self.hidden_size, self._recurrent_N, self._use_orthogonal)
                
        if self._env_name == 'MPE-simple.spread':
            self.act = mpe_act(action_space, self.hidden_size, self._use_orthogonal, self._gain)
        elif self._env_name == 'SISL-multiwalker':
            self.act = multiwalker_act(action_space, self.hidden_size, self._use_orthogonal, self._gain)

        self.to(device)
        
    def _layer_init(self, layer, std=np.sqrt(2), bias_const=0.0):
        torch.nn.init.orthogonal_(layer.weight, std)
        torch.nn.init.constant_(layer.bias, bias_const)
        return layer
    
    def forward(self, obs, rnn_states, masks, available_actions=None, deterministic=False):
        """
        Compute actions from the given inputs.
        :param obs: (np.ndarray / torch.Tensor) observation inputs into network.
        :param rnn_states: (np.ndarray / torch.Tensor) if RNN network, hidden states for RNN.
        :param masks: (np.ndarray / torch.Tensor) mask tensor denoting if hidden states should be reinitialized to zeros.
        :param available_actions: (np.ndarray / torch.Tensor) denotes which actions are available to agent
                                                              (if None, all actions available)
        :param deterministic: (bool) whether to sample from action distribution or return the mode.

        :return actions: (torch.Tensor) actions to take.
        :return action_log_probs: (torch.Tensor) log probabilities of taken actions.
        :return rnn_states: (torch.Tensor) updated RNN hidden states.
        """
        obs = check(obs).to(**self.tpdv)
        rnn_states = check(rnn_states).to(**self.tpdv)
        masks = check(masks).to(**self.tpdv)
        if available_actions is not None:
            available_actions = check(available_actions).to(**self.tpdv)
            
        print("main forward method: obs before base", obs)
        obs_isnan_mask = torch.isnan(obs)
        print("main forward method: nan mask obs", obs_isnan_mask)
        obs_num_nans = torch.sum(obs_isnan_mask)
        print("main forward method: number of nans", obs_num_nans)
        
        actor_features = self.base(obs)

        if self._use_naive_recurrent_policy or self._use_recurrent_policy:
            if self._use_rims_policy_LSTM or self._use_rims_policy_GRU:  
                half = self.hidden_size // 2
                ## RIMs
                hidden = rnn_states[:, :self.hidden_size], rnn_states[:, :self.hidden_size]
                hidden = list(hidden)
                hidden[0] = hidden[0].view(hidden[0].size(0), self._num_units, -1) ## Hidden states
                hidden[1] = hidden[0].view(hidden[1].size(0), self._num_units, -1) ## Cell states
                actor_features = actor_features.unsqueeze(1) ## Input
                ## Processing
                hidden = self.rnn(actor_features, hidden[0], hidden[1])
                hidden = list(hidden)
                actor_features = hidden[0].view(hidden[0].size(0), -1)
                rnn_states = hidden[1].view(hidden[1].size(0), 1, -1)
            elif self._use_lstm_policy:
                actor_features, rnn_states = self.rnn(actor_features, rnn_states, masks)
        actions, action_log_probs = self.act(actor_features, available_actions, deterministic)

        return actions, action_log_probs, rnn_states

    def evaluate_actions(self, obs, rnn_states, action, masks, available_actions=None, active_masks=None):
        """
        Compute log probability and entropy of given actions.
        :param obs: (torch.Tensor) observation inputs into network.
        :param action: (torch.Tensor) actions whose entropy and log probability to evaluate.
        :param rnn_states: (torch.Tensor) if RNN network, hidden states for RNN.
        :param masks: (torch.Tensor) mask tensor denoting if hidden states should be reinitialized to zeros.
        :param available_actions: (torch.Tensor) denotes which actions are available to agent
                                                              (if None, all actions available)
        :param active_masks: (torch.Tensor) denotes whether an agent is active or dead.

        :return action_log_probs: (torch.Tensor) log probabilities of the input actions.
        :return dist_entropy: (torch.Tensor) action distribution entropy for the given inputs.
        """
        obs = check(obs).to(**self.tpdv)
        rnn_states = check(rnn_states).to(**self.tpdv)
        action = check(action).to(**self.tpdv)
        masks = check(masks).to(**self.tpdv)
        if available_actions is not None:
            available_actions = check(available_actions).to(**self.tpdv)

        if active_masks is not None:
            active_masks = check(active_masks).to(**self.tpdv)
            
        print("evaluate actions: obs before base", obs)
        obs_isnan_mask = torch.isnan(obs)
        print("evaluate actions: nan mask obs", obs_isnan_mask)
        obs_num_nans = torch.sum(obs_isnan_mask)
        print("evaluate actions: number of nans", obs_num_nans)
        
        actor_features = self.base(obs)
        
        if self._use_naive_recurrent_policy or self._use_recurrent_policy:
            if self._use_rims_policy_LSTM or self._use_rims_policy_GRU:  
                half = self.hidden_size // 2
                ## RIMs
                hidden = rnn_states[:, :self.hidden_size], rnn_states[:, :self.hidden_size]
                hidden = list(hidden)
                hidden[0] = hidden[0].view(hidden[0].size(0), self._num_units, -1) ## Hidden states
                hidden[1] = hidden[0].view(hidden[1].size(0), self._num_units, -1) ## Cell states
                actor_features = actor_features.unsqueeze(1) ## Input
                ## Processing
                hidden = self.rnn(actor_features, hidden[0], hidden[1])
                hidden = list(hidden)
                actor_features = hidden[0].view(hidden[0].size(0), -1)
                rnn_states = hidden[1].view(hidden[1].size(0), 1, -1)
            elif self._use_lstm_policy:
                actor_features, rnn_states = self.rnn(actor_features, rnn_states, masks)

        action_log_probs, dist_entropy = self.act.evaluate_actions(actor_features,
                                                                   action, available_actions,
                                                                   active_masks=
                                                                   active_masks if self._use_policy_active_masks
                                                                   else None)

        return action_log_probs, dist_entropy


class R_Critic(nn.Module):
    """
    Critic network class for MAPPO. Outputs value function predictions given centralized input (MAPPO) or
                            local observations (IPPO).
    :param args: (argparse.Namespace) arguments containing relevant model information.
    :param cent_obs_space: (gym.Space) (centralized) observation space.
    :param device: (torch.device) specifies the device to run on (cpu/gpu).
    """
    def __init__(self, args, obs_space, device=torch.device("cpu")):
        super(R_Critic, self).__init__()
        self.hidden_size = args.hidden_size
        self._use_orthogonal = args.use_orthogonal
        self._use_naive_recurrent_policy = args.use_naive_recurrent_policy
        self._use_recurrent_policy = args.use_recurrent_policy
        self._use_rims_policy_LSTM = args.use_rims_policy_LSTM
        self._use_rims_policy_GRU = args.use_rims_policy_GRU
        self._use_lstm_policy = args.use_lstm_policy
        self._recurrent_N = args.recurrent_N
        self._use_popart = args.use_popart
        self._num_units = args.num_units
        self.tpdv = dict(dtype=torch.float32, device=device)
        init_method = [nn.init.xavier_uniform_, nn.init.orthogonal_][self._use_orthogonal]

        obs_shape = get_shape_from_obs_space(obs_space)
        base = CNNBase if len(obs_shape) == 3 else MLPBase
        
        base1 = nn.Sequential(
            self._layer_init(nn.Conv2d(4, 32, 3, padding=1)),
            nn.MaxPool2d(2),
            nn.ReLU(),
            self._layer_init(nn.Conv2d(32, 64, 3, padding=1)),
            nn.MaxPool2d(2),
            nn.ReLU(),
            self._layer_init(nn.Conv2d(64, 128, 3, padding=1)),
            nn.MaxPool2d(2),
            nn.ReLU(),
            nn.Flatten(),
            self._layer_init(nn.Linear(128 * 8 * 8, 512)),
            nn.ReLU(),
            self._layer_init(nn.Linear(512, 64)),
            nn.ReLU(),
        )
        
        base2 = MLPBase
        
        self.base = base1 if len(obs_shape) == 3 else base2(args, obs_shape)

        if self._use_naive_recurrent_policy or self._use_recurrent_policy:
            if self._use_rims_policy_LSTM:  
                self.rnn = RIMCell(torch.device('cuda' if torch.cuda.is_available() else 'cpu'), self.hidden_size, self.hidden_size // self._num_units, self._num_units, 1, 'LSTM', input_value_size = 64, comm_value_size = self.hidden_size // self._num_units)
            elif self._use_rims_policy_GRU:  
                self.rnn = RIMCell(torch.device('cuda' if torch.cuda.is_available() else 'cpu'), self.hidden_size, self.hidden_size // self._num_units, self._num_units, 1, 'GRU', input_value_size = 64, comm_value_size = self.hidden_size // self._num_units)
            elif self._use_lstm_policy:
                self.rnn = RNNLayer(self.hidden_size, self.hidden_size, self._recurrent_N, self._use_orthogonal)

        self.to(device)
        
        def init_(m):
            return init(m, init_method, lambda x: nn.init.constant_(x, 0))

        if self._use_popart:
            self.v_out = init_(PopArt(self.hidden_size, 1, device=device))
        else:
            self.v_out = init_(nn.Linear(self.hidden_size, 1))
            
        self.to(device)
        
    def _layer_init(self, layer, std=np.sqrt(2), bias_const=0.0):
        torch.nn.init.orthogonal_(layer.weight, std)
        torch.nn.init.constant_(layer.bias, bias_const)
        return layer
   

    def forward(self, obs, rnn_states, masks):
        """
        Compute actions from the given inputs.
        :param obs: (np.ndarray / torch.Tensor) observation inputs into network.
        :param rnn_states: (np.ndarray / torch.Tensor) if RNN network, hidden states for RNN.
        :param masks: (np.ndarray / torch.Tensor) mask tensor denoting if RNN states should be reinitialized to zeros.

        :return values: (torch.Tensor) value function predictions.
        :return rnn_states: (torch.Tensor) updated RNN hidden states.
        """
        obs = check(obs).to(**self.tpdv)
        rnn_states = check(rnn_states).to(**self.tpdv)
        masks = check(masks).to(**self.tpdv)

        critic_features = self.base(obs)
        
        if self._use_naive_recurrent_policy or self._use_recurrent_policy:
            if self._use_rims_policy_LSTM or self._use_rims_policy_GRU:  
                half = self.hidden_size // 2
                ## RIMs
                hidden = rnn_states[:, :self.hidden_size], rnn_states[:, :self.hidden_size]
                hidden = list(hidden)
                hidden[0] = hidden[0].view(hidden[0].size(0), self._num_units, -1) ## Hidden states
                hidden[1] = hidden[0].view(hidden[1].size(0), self._num_units, -1) ## Cell states
                critic_features = critic_features.unsqueeze(1) ## Input
                ## Processing
                hidden = self.rnn(critic_features, hidden[0], hidden[1])
                hidden = list(hidden)
                critic_features = hidden[0].view(hidden[0].size(0), -1)
                rnn_states = hidden[1].view(hidden[1].size(0), 1, -1)
            elif self._use_lstm_policy:
                critic_features, rnn_states = self.rnn(critic_features, rnn_states, masks)
                
        values = self.v_out(critic_features)

        return values, rnn_states
