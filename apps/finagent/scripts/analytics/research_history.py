"""Explicit research history budgets; sample scarcity never silently lowers them."""
def history_plan(min_train_sessions=500,test_sessions=80,folds=3,horizon=20):
    if horizon not in (5,20,60):raise ValueError('horizon must be 5,20,60')
    if not 60<=min_train_sessions<=2000:raise ValueError('min_train_sessions must be 60..2000')
    if not 20<=test_sessions<=252:raise ValueError('test_sessions must be 20..252')
    if not 1<=folds<=5:raise ValueError('folds must be 1..5')
    test=max(test_sessions,2*horizon)
    return {'min_train_sessions':min_train_sessions,'requested_test_sessions':test_sessions,
        'test_sessions':test,'folds':folds,'horizon':horizon,'purge_sessions':horizon,
        'required_eligible_sessions':min_train_sessions+folds*(test+horizon),
        'sample_policy':'按日期计数；同一天增加股票数不增加历史长度；不足时报告缺口'}
