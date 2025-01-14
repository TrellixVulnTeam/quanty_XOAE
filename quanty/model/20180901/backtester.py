import pandas as pd
import numpy as np
import itertools
import time
from pandas.tseries.offsets import Day
from IPython.core.debugger import set_trace
from collections import namedtuple, OrderedDict
from numba import jit, float64, types
from tqdm import tqdm, tqdm_notebook

# Custom modules
from .plotter import Plotter as pltr
#from .dual_momentum_20180808 import DualMomentum as dm
from .dual_momentum import DualMomentum as dm
from .portfolizer import Portfolio as port
from ..model import evaluator as ev
from ..model import setting



def read_db(base='prices_global.pkl', add=None):
    mapper = setting.mapper
    db = pd.read_pickle(base)
    
    if add:
        db_add = pd.read_pickle(add)
        db_add = db_add.unstack().reindex(index=db.index.levels[0], method='ffill').stack()
        db = db.append(db_add)
    
    db = db.iloc[db.index.get_level_values(1).isin(mapper.keys())]
    return db.rename(index=mapper, level=1)


def overwrite_params(base_params, **what):
    out = base_params.copy()
    out.update(what)
    return out


def dict_flatten(params, out):
    for k,v in params.items():
        if isinstance(v, dict):
            dict_flatten(v, out)
        else:
            out.update({k:v})



@jit(types.Tuple((float64[:], float64, float64[:]))(float64[:], float64[:]), nopython=True)
def _update_pos_daily_fast(pos_daily_last_np, r):
    pos_cash = 1.0 - pos_daily_last_np.sum()
    pos_updated = (1+r) * pos_daily_last_np
    pos_total = pos_cash + pos_updated.sum()
    if pos_total==0: pos_total = 1.0
    model_rtn_ = pos_total - 1.0
    model_contr_ = pos_updated - pos_daily_last_np

    return pos_updated/pos_total, model_rtn_, model_contr_



class BacktesterBase(object):
    def __init__(self, params, **opt):
        params_flat = {}
        dict_flatten(params, params_flat)
        params_flat = overwrite_params(params_flat, **opt)
        self.__dict__.update(params_flat)
        
        dates, dates_asof = self._get_dates()
        p_close, p_buy, p_sell, r = self._prices()
        self.__dict__.update({
            'dates': dates, 
            'dates_asof': dates_asof, 
            'p_close': p_close, 
            'p_buy': p_buy, 
            'p_sell': p_sell, 
            'r': r, 
        })
        
        st = time.time()
        self.dm = dm(**self.__dict__)
        print(time.time()-st)
        
        self.port = port(**self.__dict__)

        self._run()
        self.turnover = ev._turnover(self.weight)
        self.stats = ev._stats(self.cum, self.beta_to, self.stats_n_roll)

        
    def _run(self):
        raise NotImplementedError


    def _assets_all(self):
        assets_all = self.assets | {self.supporter, self.cash_equiv, self.beta_to}

        if self.bm is not None: assets_all.update({self.bm})
        if self.market is not None: assets_all.update({self.market})
        
        trade_assets = dict(self.trade_assets)
        #set_trace()
        if len(trade_assets)!=0:
            assets_all.update(set.union(*[set(trade_assets[k].keys()) for k in trade_assets.keys() if k in assets_all]))
        
        return assets_all        
        

    def _prices(self):
        assets_all = self._assets_all()
        db_unstacked = self.db.unstack().loc[:self.end].fillna(method='ffill')
        
        p_close = db_unstacked[self.price_src].reindex(columns=assets_all)
        p_high = db_unstacked['high'].reindex(columns=assets_all)
        p_low = db_unstacked['low'].reindex(columns=assets_all)
        
        p_close_high = p_close * (p_high/p_close).mean()
        p_close_low = p_close * (p_low/p_close).mean()

        p_close_high.update(p_high)
        p_close_low.update(p_low)
        
        if self.trade_tol=='at_close':
            p_buy = p_close
            p_sell = p_close
            
        elif self.trade_tol=='buyHigh_sellLow':
            p_buy = p_close_high
            p_sell = p_close_low
            
        elif self.trade_tol=='buyLow_sellHigh':
            p_buy = p_close_low
            p_sell = p_close_high
            
        return p_close, p_buy, p_sell, p_close.pct_change()    
               

    # 모든 영업일 출력
    def _get_dates(self):
        dates_all = self.db.index.levels[0]
        dates = dates_all[(self.start<=dates_all) & (dates_all<=self.end)]
        
        # 무조건 첫날(start)과 마지막날(end)은 포함
        if self.start not in dates: 
            dates = dates.insert(0, pd.Timestamp(self.start))
            
        if self.end not in dates: 
            dates = dates.append(pd.DatetimeIndex([self.end]))

        dates_asof = pd.date_range(self.start, self.end, freq='M')
        dates_asof = dates_all[dates_all.get_indexer(dates_asof, method='ffill')] & dates
        
        # 무조건 첫날(start)은 리밸 기준일
        if self.start not in dates_asof: 
            dates_asof = dates_asof.insert(0, pd.Timestamp(self.start))

        return dates, dates_asof
    
    
    def plot_cum(self, strats, **params):
        pltr.plot_cum(self.cum, strats, **params)
        
        
    def plot_cum_te(self, strats, bm, te_target, **params):
        pltr.plot_cum_te(self.cum, strats, bm, te_target, **params)
        
        
    def plot_cum_exc_te(self, strats, bm, te_target, **params):
        pltr.plot_cum_exc_te(self.cum, strats, bm, te_target, **params)
        
        
    def plot_cum_te_many(self, strats, **params):
        bm = list(self.backtests.values())[0].bm
        te_target_list = [bt.te_target for bt in self.backtests.values()]
        etas = [bt.eta for bt in self.backtests.values()]
        pltr.plot_cum_te_many(self.cum, strats, bm, te_target_list, etas, **params)
        
        
    def plot_cum_yearly(self, strats, **params): 
        pltr.plot_cum_yearly(self.cum[strats], **params)

        
    def plot_turnover(self):
        pltr.plot_turnover(self.turnover)
        
        
    def plot_weight(self, rng): 
        pltr.plot_weight(self.weight, rng, self.supporter, self.cash_equiv)
        
        
    def plot_stats(self, strats, style=None, **params):
        if style is None:
            items = {
                'cagr': 'CAGR (%)', 
                'std': 'Volatility (%)', 
                'sharpe': 'Sharpe', 
                'cagr_roll_med': 'CAGR (%,Rolling1Y)', 
                'std_roll_med': 'Volatility (%,Rolling1Y)', 
                'sharpe_roll_med': 'Sharpe (Rolling1Y)', 
                'mdd': 'MDD (%)', 
                'hit': 'Hit ratio (%,1M)', 
                'profit_to_loss': 'Profit-to-loss (%,1M)', 
                'beta': 'Beta (vs.' + self.beta_to + ')',
                'loss_proba': 'Loss probability (%,1Y)', 
                'consistency': 'Consistency (%)',
            }
            
            pltr.plot_stats(self.stats, strats, items, **params)
        
        elif style=='normal':
            items = {
                'cagr': 'CAGR (%)', 
                'std': 'Volatility (%)', 
                'sharpe': 'Sharpe', 
                'mdd': 'MDD (%)', 
                'hit': 'Hit ratio (%,1M)', 
                'profit_to_loss': 'Profit-to-loss (%,1M)', 
                'beta': 'Beta (vs.' + self.beta_to + ')',
                'loss_proba': 'Loss probability (%,1Y)', 
                'consistency': 'Consistency (%)',
            }
            
            pltr.plot_stats(self.stats, strats, items, **params)
            
        elif style=='simple':
            items = {
                'cagr': 'CAGR (%)', 
                'std': 'Volatility (%)', 
                'sharpe': 'Sharpe', 
                'mdd': 'MDD (%)', 
            }          
        
            pltr.plot_stats(self.stats, strats, items, ncols=4, **params)
        
        
    def plot_profile(self, strats, **params):
        pltr.plot_profile(self.stats, strats, **params)
        
        
    def plot_dist(self, strats, **params):
        items = {
            ev._cagr: 'CAGR (%,Rolling1Y)', 
            ev._std: 'Volatility (%,Rolling1Y)',
            ev._sharpe: 'Sharpe (Rolling1Y)', 
        }
        
        pltr.plot_dist(self.cum, strats, items, **params)
        
        
    def plot_contr_cum(self, **params):
        pltr.plot_contr_cum(self.model_contr, **params)
        
        
    def plot_breakdown(self):
        pltr.plot_breakdown(self.model_contr, self.weight)
        
        
    

class Backtester(BacktesterBase):
    def init_of_the_day(self):
        trade_amount_ = trade_cashflow_ = cost_ = 0
        
        try:
            hold_ = self.hold[-1].copy()
            cash_ = self.wealth[-1][-2]
            weight_ = self.weight[-1].copy()
            pos_ = self.pos[-1].copy()
            pos_d_ = self.pos_d[-1].copy()

        except:
            hold_ = pd.Series()
            cash_ = self.cash
            weight_ = pd.Series()
            pos_ = pd.Series()
            pos_d_ = pd.Series()

        return hold_, cash_, weight_, pos_, pos_d_, trade_amount_, trade_cashflow_, cost_
        
    
    def _run(self):
        trade_due = -1
        
        # 매일 기록
        self.hold = []
        self.eq_value = []
        self.wealth = []
        self.pos_d = []
        self.model_rtn = []
        self.model_contr = []
        
        # 의사결정일에만 기록
        self.pos = []     # 듀얼모멘텀 모델 자체에서 산출되는 비중
        self.weight = []  # 최종비중 (켈리반영)
        self.eta = []
        
        for date in tqdm_notebook(self.dates):
            if date in self.p_close.index: 
                trade_due -= 1
                
            # Initialize for today
            hold_, cash_, weight_, pos_, pos_d_, trade_amount_, trade_cashflow_, cost_ = self.init_of_the_day()
            
            
            # 0. 리밸런싱 실행하는 날
            if trade_due==0:
                trade_amount_, trade_cashflow_, cost_, cash_, hold_ = self._rebalance(date, hold_, cash_, weight_)
                pos_d_, model_rtn_, model_contr_ = self._update_pos_daily(date, pos_d_)
                pos_d_ = pos_.copy()
                

            # 1. 리밸런싱 비중결정하는 날
            elif date in self.dates_asof:
                weight_, pos_, trade_due, eta_ = self._positionize(date, weight_, trade_due)
                pos_d_, model_rtn_, model_contr_ = self._update_pos_daily(date, pos_d_)
                
                self.weight.append(weight_)
                self.pos.append(pos_)
                self.eta.append(eta_)
                
              
            # 2. 아무일도 없는 날
            else:
                pos_d_, model_rtn_, model_contr_ = self._update_pos_daily(date, pos_d_)
                

            # 종가기준 포지션 밸류 측정
            eq_value_, value_, nav_ = self._evaluate(date, hold_, cash_)
            
            
            self.hold.append(hold_)
            self.eq_value.append(eq_value_)
            self.wealth.append([trade_amount_, value_, trade_cashflow_, cost_, cash_, nav_])
            self.pos_d.append(pos_d_)
            self.model_rtn.append(model_rtn_)
            self.model_contr.append(model_contr_)


        # 종목별 시그널, 포지션
        self.weight = pd.DataFrame(self.weight, index=self.dates_asof)
        self.pos = pd.DataFrame(self.pos, index=self.dates_asof)
        self.eta = pd.DataFrame(self.eta, index=self.dates_asof)
        
        # Daily Booking
        self.hold = pd.DataFrame(self.hold, index=self.dates)
        self.eq_value = pd.DataFrame(self.eq_value, index=self.dates)
        self.wealth = pd.DataFrame(self.wealth, index=self.dates, columns=['trade_amount', 'value', 'trade_cashflow', 'cost', 'cash', 'nav'])
        self.pos_d = pd.DataFrame(self.pos_d, index=self.dates)
        self.model_rtn = pd.Series(self.model_rtn, index=self.dates)
        self.model_contr = pd.DataFrame(self.model_contr, index=self.dates)
        
        # 지수가격(normalized)
        cum = self.p_close.reindex(self.dates, method='ffill')
        cum['DualMomentum'] = self.wealth['nav']
        self.cum = cum / cum.bfill().iloc[0]
            
            
    def _update_pos_daily(self, date, pos_daily_last_):    
        if date in self.r.index:
            assets_ = pos_daily_last_.index[pos_daily_last_!=0]
            pos_daily_last_np = pos_daily_last_.loc[assets_].values
            r = self.r.loc[date, assets_].values
            
            pos_daily_last_np, model_rtn_, model_contr_ = _update_pos_daily_fast(pos_daily_last_np, r)
            
            return pd.Series(pos_daily_last_np, index=assets_), model_rtn_, pd.Series(model_contr_, index=assets_)
        
        else:
            return pos_daily_last_, 0.0, pd.Series()


    def _rebalance(self, date, hold_, cash_, pos_tobe_):
        if self.trade_prev_nav_based:
            pos_prev_amount = hold_*self.p_close.loc[:date-Day()].iloc[-1]
        else:
            pos_prev_amount = hold_*self.p_close.loc[date]
            
        # Planning
        nav_prev = pos_prev_amount.sum() + cash_
        pos_amount = self.gr_exposure * nav_prev * pos_tobe_
        pos_buffer = nav_prev - pos_amount.sum()
        amount_chg = pos_amount.sub(pos_prev_amount, fill_value=0)
        amount_buy_plan = amount_chg[amount_chg>0]
        amount_sell_plan = -amount_chg[amount_chg<0]

        # Sell first
        p_sell_ = self.p_sell.loc[date]
        share_sell = amount_sell_plan.div(p_sell_).dropna()
        share_sell.where(share_sell.lt(hold_), hold_, inplace=True)
        share_sell.where(pos_tobe_>0, hold_, inplace=True) # 비중 0는 완전히 팔아라
        amount_sell = share_sell*p_sell_
        amount_sell_sum = amount_sell.sum()
        cost_sell = amount_sell_sum * self.expense
        cash_ += (amount_sell_sum - cost_sell)

        # Buy next
        p_buy_ = self.p_buy.loc[date]
        amount_buy_plan_sum = amount_buy_plan.sum()
        amount_buy = amount_buy_plan * np.min([amount_buy_plan_sum, cash_-pos_buffer]) / amount_buy_plan_sum
        amount_buy_sum = amount_buy.sum()
        share_buy = amount_buy.div(p_buy_).dropna()
        cost_buy = amount_buy_sum * self.expense
        cash_ += (-amount_buy_sum - cost_buy)

        # 매매결과
        cost_ = cost_buy + cost_sell
        trade_cashflow_ = amount_sell_sum - amount_buy_sum
        trade_amount_ = amount_sell_sum + amount_buy_sum

        # 최종포지션
        hold_ = hold_.add(share_buy, fill_value=0).sub(share_sell, fill_value=0).dropna()
        
        return trade_amount_, trade_cashflow_, cost_, cash_, hold_


    def _positionize(self, date, weight_asis_, trade_due):
        weight_, pos_, eta_ = self.port.get(date, self.dm, self.wealth, self.model_rtn)
        
        if weight_.sub(weight_asis_, fill_value=0).abs().sum()!=0:
            trade_due = self.trade_delay

        return weight_, pos_, trade_due, eta_


    def _evaluate(self, date, hold_, cash_):
        if date==self.dates[0]:
            eq_value_ = pd.Series()
            
        else:
            eq_value_ = hold_ * self.p_close.loc[:date].iloc[-1]

        value_ = eq_value_.sum()
        nav_ = value_ + cash_
        return eq_value_, value_, nav_

                
        
class BacktestComparator(Backtester):
    def __init__(self, params, **backtests):
        params_flat = {}
        dict_flatten(params, params_flat)
        self.__dict__.update(params_flat)
        self.backtests = OrderedDict(backtests)
        self.cum, self.stats = self._get_results()


    def _get_results(self):
        for i, (k,v) in enumerate(self.backtests.items()):
            if i==0:
                cum = v.cum.copy()
                cum.rename(columns={'DualMomentum':k}, inplace=True)
              
                stats = v.stats.copy()
                stats.rename(index={'DualMomentum':k}, inplace=True)
                
            else:
                cum.loc[:,k] = v.cum.loc[:,'DualMomentum']
                stats.loc[k] = v.stats.loc['DualMomentum']
                
        return cum, stats
        
        
    def mix(self):
        self.cum = self.cum.fillna(method='ffill')
        r_mix = self.cum[list(self.backtests.keys())].pct_change()
        std_mix = r_mix.ewm(halflife=250, min_periods=20).std()
        
        dates_asof = list(self.backtests.values())[0].dates_asof
        alloc = 1.0 / std_mix.loc[dates_asof]
        alloc = alloc.div(alloc.sum(axis=1), axis=0).fillna(0.25)
        
        mixed = []
        
        for i_date, date in enumerate(self.cum.index):
            
            if i_date==0:
                cum_mix_ = alloc.loc[date]
                
            elif date in alloc.index:
                cum_mix_ = mixed[i_date-1] * (1+r_mix.loc[date])
                cum_mix_ = alloc.loc[date] * cum_mix_.sum()
                
            else:
                cum_mix_ = mixed[i_date-1] * (1+r_mix.loc[date])
                
            cum_mix_['sum'] = cum_mix_.sum()
            mixed.append(cum_mix_)
            
        mixed = pd.DataFrame(mixed, index=self.cum.index)
        self.cum['mixed'] = mixed['sum']
        self.stats = ev._stats(self.cum, self.beta_to, self.stats_n_roll)
        
        
    def plot_stats_pool(self, **params):
        items = {
            'cagr': 'CAGR(%)', 
            'std': 'Standard dev(%)', 
            'sharpe': 'Sharpe', 
            'mdd': 'MDD(%)',
        }
        
        pltr.plot_stats_pool(self.stats.loc[self.backtests.keys()], items, **params)
        
                
    @classmethod
    def compare(cls, base_params, **grids):
        params = base_params.copy()
        grid_keys = grids.keys()
        grid_values = list(itertools.product(*grids.values()))
        backtests = OrderedDict()
        bt = namedtuple('bt', grid_keys)
        
        for v in tqdm_notebook(grid_values):
            k = dict(zip(grid_keys, v))
            params.update(k)
            backtests[str(bt(**k))] = Backtester(params)
         
        return cls(params, **backtests)
      
      
    @classmethod
    def compare_highlow(cls, params):
        return cls.compare(params, trading_tolerance=['buyLow_sellHigh', 'at_close', 'buyHigh_sellLow'])
      

    def plot_cum_highlow(self, strats, **params):
        pltr.plot_cum(self.cum, strats, **params)
        plt.fill_between(self.cum.index, 
                         self.cum["bt(trading_tolerance='buyHigh_sellLow')"], 
                         self.cum["bt(trading_tolerance='buyLow_sellHigh')"], 
                         color=params['color'][-1], 
                         alpha=0.4)
