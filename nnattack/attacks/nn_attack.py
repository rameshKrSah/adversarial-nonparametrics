import itertools
import tempfile
import os
import warnings
warnings.simplefilter(action='ignore', category=FutureWarning)

import scipy.sparse as ss
from cvxopt import matrix, solvers
import cvxopt.glpk
import cvxopt
import numpy as np
from sklearn.neighbors import KDTree
import scipy
import joblib
from joblib import Parallel, delayed
from sklearn.neighbors import KNeighborsClassifier
from bistiming import IterTimer

from .cutils import c_get_half_space, get_all_half_spaces, get_constraints, check_feasibility

import logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

#import cvxopt.msk
#from mosek import iparam
#solvers.options['MOSEK'] = {iparam.log: 0}

#import mosek
#msk.options = {mosek.iparam.log: 0}
#solvers.options['solver'] = 'glpk'
#solvers.options['solver'] = 'mosek'
solvers.options['maxiters'] = 30
solvers.options['show_progress'] = False
solvers.options['refinement'] = 0
solvers.options['feastol'] = 1e-7
solvers.options['abstol'] = 1e-7
solvers.options['reltol'] = 1e-7
cvxopt.glpk.options["msg_lev"] = "GLP_MSG_OFF"

def get_half_space(a, b):
    w = (b - a)
    c = np.dot(w.T, (a + b) / 2)
    sign = -np.sign(np.dot(w.T, b) - c)
    w = sign * w
    c = sign * c
    return [w, c]

glob_trnX = None
glob_trny = None

DEBUG = False

import cvxpy as cp

def solve_lp(c, G, h, n):
    c = np.array(c)
    G, h = np.array(G), np.array(h)
    x = cp.Variable(shape=(n, 1))
    obj = cp.Minimize(c.T * x)
    constraints = [G*x <= h]
    prob = cp.Problem(obj, constraints)
    prob.solve(solver=cp.GUROBI)
    return prob.status, x.value


#@profile
def get_sol(target_x, tuple_x, faropp, kdtree, transformer, init_x=None):
    tuple_x = np.asarray(tuple_x)
    trnX = glob_trnX.dot(transformer.T)
    emb_tar = target_x.dot(transformer.T)
    G, h, _ = get_constraints(trnX, tuple_x, kdtree, faropp, emb_tar)
    G, h = matrix(G, tc='d'), matrix(h, tc='d')

    #assert (transformer.shape[1] == glob_trnX.shape[1])
    #n_emb = transformer.shape[0]
    n_fets = target_x.shape[0]

    #Q = 2 * matrix(np.eye(n_emb), tc='d')
    Q = 2 * matrix(np.eye(n_fets), tc='d')
    #Q = 2 * matrix(np.dot(np.dot(transformer.T, np.eye(n_emb)), transformer), tc='d')
    T = matrix(transformer.astype(np.float64), tc='d')

    G = G * T
    #Q = T.trans() * Q * T
    #q = matrix(-2*target_x.dot(transformer.T).dot(transformer), tc='d')
    q = matrix(-2*target_x, tc='d')

    try:
        c = matrix(np.zeros(target_x.shape[0]), tc='d')
        temph = h - 1e-4 # make sure all constraints are met
        if init_x is None:
            lp_sol = solvers.lp(c=c, G=G, h=temph, solver='glpk')
            if lp_sol['status'] == 'optimal':
                init_x = lp_sol['x']
            else:
                init_x = None

        if init_x is not None:
            #sol2 = solve_qp(np.array(Q), np.array(q).flatten(), np.array(G).T,
            #        np.array(temph).flatten())
            sol = solvers.qp(P=Q, q=q, G=G, h=temph, initvals=init_x)

            if sol['status'] == 'optimal':
                ret = np.array(sol['x'], np.float64).reshape(-1)
                if DEBUG:
                    # sanity check for the correctness of objective
                    print('1', sol['primal objective'] + np.dot(target_x, target_x))
                    print('2', np.linalg.norm(target_x - ret, ord=2)**2)
                    #print(sol['primal objective'], np.dot(target_x.dot(transformer.T), target_x.dot(transformer.T)))
                    #print(sol['primal objective'] + np.dot(target_x.dot(transformer.T), target_x.dot(transformer.T)))
                    #print(np.linalg.norm(target_x.dot(transformer.T) - ret.dot(transformer.T))**2)
                    #assert np.isclose(np.linalg.norm(target_x.dot(transformer.T) - ret.dot(transformer.T))**2,
                    #        sol['primal objective'] + np.dot(target_x.dot(transformer.T), target_x.dot(transformer.T)))
                    assert np.isclose(np.linalg.norm(target_x - ret, ord=2)**2, sol['primal objective'] + np.dot(target_x, target_x))
                    # check if constraints are all met
                    h = np.array(h).flatten()
                    G = np.array(G)
                    a = check_feasibility(G, h, ret, G.shape[0], G.shape[1])
                    assert a
                return True, ret
            return False, np.array(init_x, np.float64).reshape(-1)
        else:
            return False, None

    except ValueError:
        #logger.warning("solver error")
        return False, None

def sol_sat_constraints(G, h):
    fet_dim = G.shape[1]
    c = matrix(np.zeros(fet_dim), tc='d')
    G = matrix(G, tc='d')
    temph = matrix(h - 1e-4, tc='d')
    sol = solvers.lp(c=c, G=G, h=temph, solver='glpk')
    return (sol['status'] == 'optimal')


def get_sol_l1(target_x, tuple_x, faropp, kdtree, transformer, init_x=None):
    tuple_x = np.asarray(tuple_x)
    fet_dim = target_x.shape[0]
    #n_emb = transformer.shape[0]

    emb_tar = target_x.dot(transformer.T)
    trnX = glob_trnX.dot(transformer.T)
    G, h, dist = get_constraints(trnX, tuple_x, kdtree, faropp, emb_tar)
    G = np.dot(G, transformer)

    if init_x is None and not sol_sat_constraints(G, h):
        return False, None

    c = matrix(np.concatenate((np.zeros(fet_dim), np.ones(fet_dim))), tc='d')

    G = np.hstack((G, np.zeros((G.shape[0], fet_dim))))
    #G = np.vstack((G, np.hstack((transformer, -np.eye(fet_dim)))))
    #G = np.vstack((G, np.hstack((-transformer, -np.eye(fet_dim)))))
    G = np.vstack((G, np.hstack((np.eye(fet_dim), -np.eye(fet_dim)))))
    G = np.vstack((G, np.hstack((-np.eye(fet_dim), -np.eye(fet_dim)))))

    #h = np.concatenate((h, emb_tar, -emb_tar))
    h = np.concatenate((h, target_x, -target_x))

    G, h = matrix(G, tc='d'), matrix(h, tc='d')

    temph = h - 1e-4
    if init_x is not None:
        sol = solvers.lp(c=c, G=G, h=temph, solver='glpk',
                         initvals=init_x)
    else:
        sol = solvers.lp(c=c, G=G, h=temph, solver='glpk')

    if sol['status'] == 'optimal':
        ret = np.array(sol['x']).reshape(-1)
        ### sanity check for the correctness of objective
        if DEBUG:
            # check if constraints are all met
            h = np.array(h).flatten()
            G = np.array(G)
            a = check_feasibility(G, h, ret, G.shape[0], G.shape[1])
            print(a)
            print('1', sol['primal objective'])
            print('2', np.linalg.norm(target_x - ret[:len(ret)//2], ord=1))
            print('3', np.linalg.norm(target_x.dot(transformer.T) - (ret[:len(ret)//2]).dot(transformer.T), ord=1))
            #print(target_x.dot(transformer.T))
            #print(ret)
            #assert np.isclose(np.linalg.norm(target_x.dot(transformer.T) - (ret[:len(ret)//2]).dot(transformer.T), ord=1),
            #                  sol['primal objective'], rtol=1e-4)
            assert np.isclose(np.linalg.norm(target_x - ret[:len(ret)//2], ord=1),
                              sol['primal objective'], rtol=1e-4)
        return True, ret[:len(ret)//2]
    else:
        #logger.warning("solver error")
        return False, None

#@profile
def get_sol_linf(target_x, tuple_x, faropp, kdtree, transformer, init_x=None):
    tuple_x = np.asarray(tuple_x)
    fet_dim = target_x.shape[0]
    #n_emb = transformer.shape[0]

    emb_tar = target_x.dot(transformer.T)
    trnX = glob_trnX.dot(transformer.T)
    G, h, _ = get_constraints(trnX, tuple_x, kdtree, faropp, emb_tar)
    G = np.dot(G, transformer)

    if init_x is None and not sol_sat_constraints(G, h):
        return False, None

    c = matrix(np.concatenate((np.zeros(fet_dim), np.ones(1))), tc='d')

    G2 = np.hstack((np.eye(fet_dim), -np.ones((fet_dim, 1))))
    G3 = np.hstack((-np.eye(fet_dim), -np.ones((fet_dim, 1))))
    G = np.hstack((G, np.zeros((G.shape[0], 1))))
    G = np.vstack((G, G2, G3))
    h = np.concatenate((h, target_x, -target_x))

    G, h = matrix(G, tc='d'), matrix(h, tc='d')

    temph = h - 1e-4

    #status, sol = solve_lp(c=c, G=G, h=temph, n=len(c))
    #if status == 'optimal':
    #    ret = np.array(sol).reshape(-1)
    #    return True, ret[:-1]
    #else:
    #    #logger.warning("solver error")
    #    return False, None

    if init_x is not None:
        sol = solvers.lp(c=c, G=G, h=temph, solver='glpk',
                         initvals=init_x)
    else:
        sol = solvers.lp(c=c, G=G, h=temph, solver='glpk')
    if sol['status'] == 'optimal':
        ret = np.array(sol['x']).reshape(-1)
        ### sanity check for the correctness of objective
        if DEBUG:
            # check if constraints are all met
            h = np.array(h).flatten()
            G = np.array(G)
            a = check_feasibility(G, h, ret, G.shape[0], G.shape[1])
            print(a)
            print('1', sol['primal objective'])
            print('2', np.linalg.norm(target_x - ret[:-1], ord=np.inf))
            print('3', np.linalg.norm(target_x.dot(transformer.T) - (ret[:-1]).dot(transformer.T), ord=np.inf))
            #print(target_x.dot(transformer.T))
            #print(ret)
            #assert np.isclose(np.linalg.norm(target_x.dot(transformer.T) - (ret[:-1]).dot(transformer.T), ord=np.inf),
            #                  sol['primal objective'], rtol=1e-4)
        return True, ret[:-1]
    else:
        #logger.warning("solver error")
        return False, None


#@profile
def get_adv(target_x, target_y, kdtree, farthest, n_neighbors, faropp,
        transformer, lp_sols, ord=2):
    ind = kdtree.query(target_x.dot(transformer.T).reshape((1, -1)),
                       k=n_neighbors, return_distance=False)[0]
    if target_y != np.argmax(np.bincount(glob_trny[ind])):
        # already incorrectly predicted
        return np.zeros_like(target_x)

    temp = (target_x, np.inf)
    if farthest == -1:
        farthest = glob_trnX.shape[0]
        ind = np.arange(glob_trnX.shape[0])
    else:
        ind = kdtree.query(target_x.dot(transformer.T).reshape((1, -1)),
                        k=farthest, return_distance=False)
        ind = ind[0]

    combs = []
    for comb in itertools.combinations(range(farthest), n_neighbors):
        comb = list(comb)
        if target_y != np.argmax(np.bincount(glob_trny[ind[comb]])):
            combs.append(comb)

    knn = KNeighborsClassifier(n_neighbors=n_neighbors)
    knn.fit(glob_trnX.dot(transformer.T), glob_trny)

    if ord == 1:
        get_sol_fn = get_sol_l1
    elif ord == 2:
        get_sol_fn = get_sol
    elif ord == np.inf:
        get_sol_fn = get_sol_linf
    else:
        raise ValueError("Unsupported ord %d" % ord)

    for comb in combs:
        comb_tup = tuple(ind[comb])

        if comb_tup not in lp_sols:
            ret, sol = get_sol_fn(target_x, ind[comb], faropp, kdtree,
                                    transformer)
            lp_sols[comb_tup] = sol
        elif lp_sols[comb_tup] is None:
            ret = False
        else:
            ret, sol = get_sol_fn(target_x, ind[comb], faropp, kdtree,
                                transformer, lp_sols[comb_tup])

        if ret:
            if knn.predict(sol.reshape(1, -1).dot(transformer.T))[0] == target_y:
                print("shouldn't happend")
                assert False
            else:
                eps = np.linalg.norm(sol - target_x, ord=ord)
                if eps < temp[1]:
                    temp = (sol, eps)

            if DEBUG:
                a = knn.predict(np.dot(target_x.reshape(1, -1), transformer.T))[0]
                b = knn.predict(np.dot(sol.reshape(1, -1), transformer.T))[0]
                print(a, b, target_y)
                if a == b and a == target_y:
                    print("shouldn't happend")
                    assert False
                #get_sol(target_x, ind[comb], faropp, kdtree, transformer)

    return temp[0] - target_x

class NNAttack():
    def __init__(self, trnX, trny, n_neighbors=3, farthest=-1, faropp=-1,
            transformer=None, ord=2):
        #furthest >= K
        self.K = n_neighbors
        self.trnX = trnX
        self.trny = trny
        self.farthest = min(farthest, len(trnX))
        self.faropp = faropp
        self.transformer = transformer
        self.ord = ord
        if transformer is not None:
            self.tree = KDTree(self.transformer.transform(self.trnX))
        else:
            self.tree = KDTree(self.trnX)
        self.lp_sols = {}
        print(np.shape(self.trnX), len(self.trny))

    #@profile
    def perturb(self, X, y, eps=None, logging=False, n_jobs=1):
        if logging:
            self.logs = {
                'local_opt': [],
                'tuple_count': 0,
            }
        if self.transformer:
            transformer = self.transformer.transformer()
        else:
            transformer = np.eye(self.trnX.shape[1])

        global glob_trnX
        global glob_trny
        glob_trnX = self.trnX
        glob_trny = self.trny

        #ret = Parallel(n_jobs=-1, backend="threading", batch_size='auto',
        #               verbose=5)(
        #    delayed(get_adv)(target_x, target_y, self.tree, self.farthest, self.K,
        #                     self.faropp, transformer, self.lp_sols,
        #                     ord=self.ord) for target_x, target_y in zip(X, y))

        #knn = KNeighborsClassifier(n_neighbors=self.K)
        #knn.fit(glob_trnX.dot(transformer.T), glob_trny)

        ret = []
        with IterTimer("Perturbing", len(X)) as timer:
            for i, (target_x, target_y) in enumerate(zip(X, y)):
                timer.update(i)
                #ret.append(get_adv(target_x, target_y, self.tree,
                ret.append(get_adv(target_x.astype(np.float64), target_y, self.tree,
                                   self.farthest, self.K, self.faropp,
                                   transformer, self.lp_sols, ord=self.ord))

        ret = np.asarray(ret)
        if isinstance(eps, list):
            rret = []
            norms = np.linalg.norm(ret, axis=1, ord=self.ord)
            for ep in eps:
                t = np.copy(ret)
                t[norms > ep, :] = 0
                rret.append(t)
            return rret
        elif eps is not None:
            ret[np.linalg.norm(ret, axis=1, ord=self.ord) > eps, :] = 0
            return ret
        else:
            return ret

#@profile
def rev_get_adv(target_x, target_y, kdtree, farthest, n_neighbors, faropp,
        transformer, lp_sols, ord=2, method='self', knn=None):
    if farthest == -1:
        farthest = glob_trnX.shape[0]
    temp = (target_x, np.inf)

    # already predicted wrong
    if knn.predict(target_x.dot(transformer.T).reshape((1, -1)))[0] != target_y:
        return temp[0] - target_x

    if ord == 1:
        get_sol_fn = get_sol_l1
    elif ord == 2:
        get_sol_fn = get_sol
    elif ord == np.inf:
        get_sol_fn = get_sol_linf
    else:
        raise ValueError("Unsupported ord %d" % ord)

    #knn = KNeighborsClassifier(n_neighbors=n_neighbors)
    #knn.fit(glob_trnX.dot(transformer.T), glob_trny)

    ind = kdtree.query(target_x.dot(transformer.T).reshape((1, -1)),
                       k=len(glob_trnX), return_distance=False)[0]
    ind = list(filter(lambda x: glob_trny[x] != target_y, ind))[:farthest]

    for i in ind:
        if method == 'self':
            inds = [i]
        elif method == 'region':
            procedX = glob_trnX[i].dot(transformer.T).reshape((1, -1))
            inds = kdtree.query(procedX, k=n_neighbors, return_distance=False)[0]
        inds = tuple([_ for _ in inds])

        if inds not in lp_sols:
            ret, sol = get_sol_fn(target_x, inds, faropp, kdtree, transformer)
            lp_sols[inds] = sol
        elif lp_sols[inds] is None:
            ret = False
            #ret, sol = get_sol_fn(target_x, inds, faropp, kdtree,
            #                        transformer)
        else:
            ret, sol = get_sol_fn(target_x, inds, faropp, kdtree,
                                    transformer, lp_sols[inds])

        if ret:
            proc = np.array([sol]).dot(transformer.T)
            if knn.predict(proc)[0] != target_y:
                eps = np.linalg.norm(sol - target_x, ord=ord)
                if eps < temp[1]:
                    temp = (sol, eps)

    return temp[0] - target_x

class RevNNAttack():
    def __init__(self, trnX, trny, n_neighbors=3, farthest=5, faropp=-1,
            transformer=None, ord=2, method='self'):
        #furthest >= K
        self.K = n_neighbors
        self.trnX = trnX
        self.trny = trny
        self.faropp = faropp
        self.farthest = farthest
        self.method = method
        self.transformer = transformer
        self.ord = ord
        if transformer is not None:
            self.tree = KDTree(self.transformer.transform(self.trnX))
        else:
            self.tree = KDTree(self.trnX)
        self.lp_sols = {}

    #@profile
    def perturb(self, X, y, eps=None, logging=False, n_jobs=1):
        if self.transformer:
            transformer = self.transformer.transformer()
        else:
            transformer = np.eye(self.trnX.shape[1])

        global glob_trnX
        global glob_trny
        glob_trnX = self.trnX
        glob_trny = self.trny

        knn = KNeighborsClassifier(n_neighbors=self.K)
        knn.fit(glob_trnX.dot(transformer.T), glob_trny)

        ret = []
        with IterTimer("Perturbing", len(X)) as timer:
            for i, (target_x, target_y) in enumerate(zip(X, y)):
                timer.update(i)
                ret.append(rev_get_adv(target_x.astype(np.float64), target_y,
                        self.tree, self.farthest, self.K, self.faropp, transformer,
                        self.lp_sols, ord=self.ord, method=self.method, knn=knn))
                #if np.linalg.norm(ret[-1]) == 0 and knn.predict([target_x]) == target_y:
                #    import ipdb; ipdb.set_trace()

        ret = np.asarray(ret)
        self.perts = ret
        if isinstance(eps, list):
            rret = []
            norms = np.linalg.norm(ret, axis=1, ord=self.ord)
            for ep in eps:
                t = np.copy(ret)
                t[norms > ep, :] = 0
                rret.append(t)
            return rret
        elif eps is not None:
            ret[np.linalg.norm(ret, axis=1, ord=self.ord) > eps, :] = 0
            return ret
        else:
            return ret
