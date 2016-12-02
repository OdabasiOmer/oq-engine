# -*- coding: utf-8 -*-
# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
# Copyright (C) 2015-2016 GEM Foundation
#
# OpenQuake is free software: you can redistribute it and/or modify it
# under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# OpenQuake is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with OpenQuake. If not, see <http://www.gnu.org/licenses/>.
from __future__ import division
import logging
import operator

import numpy

from openquake.baselib.python3compat import zip
from openquake.baselib.general import (
    AccumDict, humansize, block_splitter, group_array)
from openquake.calculators import base, event_based
from openquake.baselib import parallel
from openquake.risklib import scientific, riskinput
from openquake.baselib.parallel import starmap

U32 = numpy.uint32
F32 = numpy.float32
F64 = numpy.float64
getweight = operator.attrgetter('weight')


def build_el_dtypes(insured_losses):
    """
    :param bool insured_losses:
        job.ini configuration parameter
    :returns:
        ela_dt and elt_dt i.e. the data types for event loss assets and
        event loss table respectively
    """
    I = insured_losses + 1
    ela_list = [('eid', U32), ('aid', U32), ('loss', (F32, I))]
    elt_list = [('eid', U32), ('loss', (F32, I))]
    return numpy.dtype(ela_list), numpy.dtype(elt_list)


def build_agg_curve(lr_data, insured_losses, ses_ratio, curve_resolution, L,
                    monitor):
    """
    Build the aggregate loss curve in parallel for each loss type
    and realization pair.

    :param lr_data:
        a list of triples `(l, r, data)` where `l` is the loss type index,
        `r` is the realization index and `data` is an array of kind
        `(rupture_id, loss)` or `(rupture_id, loss, loss_ins)`
    :param bool insured_losses:
        job.ini configuration parameter
    :param ses_ratio:
        a ratio obtained from ses_per_logic_tree_path
    :param curve_resolution:
        the number of discretization steps for the loss curve
    :param L:
        the number of loss types
    :param monitor:
        a Monitor instance
    :returns:
        a dictionary (r, l, i) -> (losses, poes, avg)
    """
    result = {}
    for l, r, data in lr_data:
        if len(data) == 0:  # realization with no losses
            continue
        if insured_losses:
            gloss = data['loss'][:, 0]
            iloss = data['loss'][:, 1]
        else:
            gloss = data['loss']
        losses, poes = scientific.event_based(
            gloss, ses_ratio, curve_resolution)
        avg = scientific.average_loss((losses, poes))
        result[l, r, 'losses'] = losses
        result[l, r, 'poes'] = poes
        result[l, r, 'avg'] = avg
        if insured_losses:
            losses_ins, poes_ins = scientific.event_based(
                iloss, ses_ratio, curve_resolution)
            avg_ins = scientific.average_loss((losses_ins, poes_ins))
            result[l, r, 'losses_ins'] = losses_ins
            result[l, r, 'poes_ins'] = poes_ins
            result[l, r, 'avg_ins'] = avg_ins
    return result


def square(L, R, factory):
    """
    :param L: the number of loss types
    :param R: the number of realizations
    :param factory: thunk used to initialize the elements
    :returns: a numpy matrix of shape (L, R)
    """
    losses = numpy.zeros((L, R), object)
    for l in range(L):
        for r in range(R):
            losses[l, r] = factory()
    return losses


def _old_loss_curves(asset_values, rcurves, ratios):
    # build loss curves in the old format (i.e. (losses, poes)) from
    # loss curves in the new format (i.e. poes).
    # shape (N, 2, C)
    return numpy.array([(avalue * ratios, poes)
                        for avalue, poes in zip(asset_values, rcurves)])


def _aggregate(outputs, compositemodel, agg, ass, idx, result, monitor):
    # update the result dictionary and the agg array with each output
    lrs = set()
    for out in outputs:
        l, r = out.lr
        lrs.add(out.lr)
        loss_type = compositemodel.loss_types[l]
        indices = numpy.array([idx[eid] for eid in out.eids])
        agglr = agg[l, r]
        for i, asset in enumerate(out.assets):
            aid = asset.ordinal
            loss_ratios = out.loss_ratios[i]
            losses = loss_ratios * asset.value(loss_type)

            # average losses
            if monitor.avg_losses:
                result['avglosses'][l, r][aid] += (
                    loss_ratios.sum(axis=0) * monitor.ses_ratio)

            # asset losses
            if monitor.loss_ratios:
                data = [(eid, aid, loss)
                        for eid, loss in zip(out.eids, loss_ratios)
                        if loss.sum() > 0]
                if data:
                    ass[l, r].append(numpy.array(data, monitor.ela_dt))

            # agglosses
            agglr[indices] += losses
    return sorted(lrs)


def event_based_risk(riskinput, riskmodel, assetcol, monitor):
    """
    :param riskinput:
        a :class:`openquake.risklib.riskinput.RiskInput` object
    :param riskmodel:
        a :class:`openquake.risklib.riskinput.CompositeRiskModel` instance
    :param assetcol:
        AssetCollection instance
    :param monitor:
        :class:`openquake.baselib.performance.Monitor` instance
    :returns:
        a dictionary of numpy arrays of shape (L, R)
    """
    A = len(assetcol)
    I = monitor.insured_losses + 1
    eids = riskinput.eids
    E = len(eids)
    idx = dict(zip(eids, range(E)))
    agg = AccumDict(accum=numpy.zeros((E, I), F32))
    ass = AccumDict(accum=[])
    result = dict(agglosses=AccumDict(), asslosses=AccumDict())
    if monitor.avg_losses:
        result['avglosses'] = AccumDict(accum=numpy.zeros((A, I), F64))

    outputs = riskmodel.gen_outputs(riskinput, monitor, assetcol)
    lrs = _aggregate(outputs, riskmodel, agg, ass, idx, result, monitor)
    for lr in lrs:
        records = [(eids[i], loss) for i, loss in enumerate(agg[lr])
                   if loss.sum() > 0]
        if records:
            result['agglosses'][lr] = numpy.array(records, monitor.elt_dt)
    for lr in ass:
        if ass[lr]:
            result['asslosses'][lr] = numpy.concatenate(ass[lr])

    # store the size of the GMFs
    result['gmfbytes'] = monitor.gmfbytes
    return result


@base.calculators.add('event_based_risk')
class EbrPostCalculator(base.RiskCalculator):
    pre_calculator = 'ebrisk'

    def execute(self):
        A = len(self.assetcol)
        self.loss_curve_dt, self.loss_maps_dt = (
            scientific.build_loss_dtypes(
                self.oqparam.loss_ratios,
                self.oqparam.conditional_loss_poes,
                self.oqparam.insured_losses + 1))

        ltypes = self.riskmodel.loss_types
        I = self.oqparam.insured_losses + 1
        R = len(self.rlzs_assoc.realizations)
        self.vals = self.assetcol.values()

        # loss curves
        multi_lr_dt = numpy.dtype(
            [(ltype, (F32, cbuilder.curve_resolution))
             for ltype, cbuilder in zip(
                ltypes, self.riskmodel.curve_builders)])
        # TODO: change 2 -> I, then change the exporter
        rcurves = numpy.zeros((A, R, 2), multi_lr_dt)

        if self.oqparam.loss_ratios:
            self.save_rcurves(rcurves, I)

        if self.oqparam.conditional_loss_poes:
            self.save_loss_maps(A, R)

        self.build_stats()

    def post_execute(self):
        pass

    def save_rcurves(self, rcurves, I):
        assets = list(self.assetcol)
        with self.monitor('building rcurves-rlzs'):
            for rlzname in self.datastore['ass_loss_ratios']:
                r = int(rlzname[4:])  # strip "rlz-"
                for cb in self.riskmodel.curve_builders:
                    try:
                        data = self.datastore['ass_loss_ratios/%s/%s' %
                                              (rlzname, cb.loss_type)].value
                    except KeyError:  # no data for the given rlz, ltype
                        continue
                    if cb.user_provided:
                        aids, curves = cb(
                            assets, group_array(data, 'aid'),
                            self.oqparam.ses_ratio)
                        if not len(aids):  # no curve
                            continue
                        A, L = curves.shape[:2]
                        rcurves[cb.loss_type][aids, r] = curves.reshape(
                            A, I, L)
            self.datastore['rcurves-rlzs'] = rcurves

    def save_loss_maps(self, N, R):
        with self.monitor('building loss_maps-rlzs'):
            if (self.oqparam.conditional_loss_poes and
                    'rcurves-rlzs' in self.datastore):
                loss_maps = numpy.zeros((N, R), self.loss_maps_dt)
                rcurves = self.datastore['rcurves-rlzs']
                for cb in self.riskmodel.curve_builders:
                    if cb.user_provided:
                        lm = loss_maps[cb.loss_type]
                        for r, lmaps in cb.build_loss_maps(
                                self.assetcol.array, rcurves):
                            lm[:, r] = lmaps
                self.datastore['loss_maps-rlzs'] = loss_maps

    def _collect_all_data(self):
        # called only if 'rcurves-rlzs' in dstore; return a list of outputs
        data_by_lt = {}
        assets = self.datastore['asset_refs'].value[self.assetcol.array['idx']]
        A = len(assets)
        rlzs = self.rlzs_assoc.realizations
        insured = self.oqparam.insured_losses
        if self.oqparam.avg_losses:
            avg_losses = self.datastore['avg_losses-rlzs'].value
        r_curves = self.datastore['rcurves-rlzs'].value
        L = len(self.riskmodel.lti)
        for l, cbuilder in enumerate(self.riskmodel.curve_builders):
            loss_type = cbuilder.loss_type
            rcurves = r_curves[loss_type]
            asset_values = self.vals[loss_type]
            data = []
            for rlz in rlzs:
                if self.oqparam.avg_losses:
                    average_losses = avg_losses[:, rlz.ordinal, l]
                    average_insured_losses = (
                        avg_losses[:, rlz.ordinal, l + L] if insured else None)
                else:
                    average_losses = numpy.zeros(A, F32)
                    average_insured_losses = numpy.zeros(A, F32)
                loss_curves = _old_loss_curves(
                    asset_values, rcurves[:, rlz.ordinal, 0], cbuilder.ratios)
                insured_curves = _old_loss_curves(
                    asset_values, rcurves[:, rlz.ordinal, 1],
                    cbuilder.ratios) if insured else None
                out = scientific.Output(
                    assets, loss_type, rlz.ordinal, rlz.weight,
                    loss_curves=loss_curves,
                    insured_curves=insured_curves,
                    average_losses=average_losses,
                    average_insured_losses=average_insured_losses)
                data.append(out)
            data_by_lt[loss_type] = data
        return data_by_lt

    # NB: the HDF5 structure is of kind <output>-stats/structural/mean, ...
    # and must be so for the loss curves, since different loss_types may have
    # a different discretization. This is not needed for the loss maps, but it
    # is done anyway for consistency, also because in the future we could
    # specify different conditional loss poes depending on the loss type
    def compute_store_stats(self, rlzs, builder):
        """
        Compute and store the statistical outputs.
        :param rlzs: list of realizations
        """
        oq = self.oqparam
        data_by_lt = self._collect_all_data()
        if not data_by_lt:
            return
        sb = scientific.StatsBuilder(
            oq.quantile_loss_curves, oq.conditional_loss_poes, [],
            oq.loss_curve_resolution, scientific.normalize_curves_eb,
            oq.insured_losses)
        loss_curves, loss_maps = sb.get_curves_maps(data_by_lt, oq.loss_ratios)
        self.datastore['loss_curves-stats'] = loss_curves
        if oq.conditional_loss_poes:
            self.datastore['loss_maps-stats'] = loss_maps

    def build_agg_curve(self):
        """
        Build a single loss curve per realization. It is NOT obtained
        by aggregating the loss curves; instead, it is obtained without
        generating the loss curves, directly from the the aggregate losses.
        """
        agg_loss_table = self.datastore['agg_loss_table']
        oq = self.oqparam
        C = oq.loss_curve_resolution
        loss_curve_dt, _ = self.riskmodel.build_all_loss_dtypes(
            C, oq.conditional_loss_poes, oq.insured_losses)
        lts = self.riskmodel.loss_types
        lr_data = []
        R = len(self.rlzs_assoc.realizations)
        L = len(self.riskmodel.lti)
        for rlzstr in agg_loss_table:
            r = int(rlzstr[4:])
            for lt, dset in agg_loss_table[rlzstr].items():
                l = self.riskmodel.lti[lt]
                lr_data.append((l, r, dset.value))
        ses_ratio = self.oqparam.ses_ratio
        I = self.oqparam.insured_losses
        result = parallel.apply(
            build_agg_curve, (lr_data, I, ses_ratio, C, L, self.monitor('')),
            concurrent_tasks=self.oqparam.concurrent_tasks).reduce()
        agg_curve = numpy.zeros(R, loss_curve_dt)
        for l, r, name in result:
            agg_curve[lts[l]][name][r] = result[l, r, name]
        self.datastore['agg_curve-rlzs'] = agg_curve

    def build_stats(self):
        oq = self.datastore['oqparam']
        builder = scientific.StatsBuilder(
            oq.quantile_loss_curves, oq.conditional_loss_poes, [],
            oq.loss_curve_resolution, scientific.normalize_curves_eb,
            oq.insured_losses)

        # build an aggregate loss curve per realization plus statistics
        if 'agg_loss_table' in self.datastore:
            with self.monitor('building agg_curve'):
                self.build_agg_curve()

        rlzs = self.datastore['csm_info'].get_rlzs_assoc().realizations
        if len(rlzs) > 1:
            with self.monitor('computing stats'):
                if 'rcurves-rlzs' in self.datastore:
                    self.compute_store_stats(rlzs, builder)


elt_dt = numpy.dtype([('eid', U32), ('loss', F32)])

save_events = event_based.EventBasedRuptureCalculator.__dict__['save_events']


class EpsilonMatrix0(object):
    """
    Mock-up for a matrix of epsilons of size N x E,
    used when asset_correlation=0.

    :param num_assets: N assets
    :param seeds: E seeds, set before calling numpy.random.normal
    """
    def __init__(self, num_assets, seeds):
        self.num_assets = num_assets
        self.seeds = seeds
        self.eps = None

    def make_eps(self):
        """
        Builds a matrix of N x E epsilons
        """
        eps = numpy.zeros((self.num_assets, len(self.seeds)), F32)
        for i, seed in enumerate(self.seeds):
            numpy.random.seed(seed)
            eps[:, i] = numpy.random.normal(size=self.num_assets)
        return eps

    def __getitem__(self, item):
        if self.eps is None:
            self.eps = self.make_eps()
        return self.eps[item]


class EpsilonMatrix1(object):
    """
    Mock-up for a matrix of epsilons of size N x E,
    used when asset_correlation=1.

    :param num_events: number of events
    :param seed: seed used to generate E epsilons
    """
    def __init__(self, num_events, seed):
        self.num_events = num_events
        self.seed = seed
        numpy.random.seed(seed)
        self.eps = numpy.random.normal(size=num_events)

    def __getitem__(self, item):
        # item[0] is the asset index, item[1] the event index
        # the epsilons are equal for all assets since asset_correlation=1
        return self.eps[item[1]]


@base.calculators.add('ebrisk')
class EbriskCalculator(base.RiskCalculator):
    """
    Event based PSHA calculator generating the total losses by taxonomy
    """
    pre_calculator = 'event_based_rupture'
    is_stochastic = True

    # TODO: if the number of source models is larger than concurrent_tasks
    # a different strategy should be used; the one used here is good when
    # there are few source models, so that we cannot parallelize on those
    def build_starmap(self, sm_id, ruptures_by_grp, sitecol,
                      assetcol, riskmodel, imts, trunc_level, correl_model,
                      min_iml, monitor):
        """
        :param sm_id: source model ordinal
        :param ruptures_by_grp: dictionary of ruptures by src_group_id
        :param sitecol: a SiteCollection instance
        :param assetcol: an AssetCollection instance
        :param riskmodel: a RiskModel instance
        :param imts: a list of Intensity Measure Types
        :param trunc_level: truncation level
        :param correl_model: correlation model
        :param min_iml: vector of minimum intensities, one per IMT
        :param monitor: a Monitor instance
        :returns: a pair (starmap, dictionary of attributes)
        """
        csm_info = self.csm_info.get_info(sm_id)
        grp_ids = sorted(csm_info.get_sm_by_grp())
        rlzs_assoc = csm_info.get_rlzs_assoc(
            count_ruptures=lambda grp: len(ruptures_by_grp.get(grp.id, [])))
        num_events = sum(ebr.multiplicity for grp in ruptures_by_grp
                         for ebr in ruptures_by_grp[grp])
        seeds = self.oqparam.random_seed + numpy.arange(num_events)

        allargs = []
        # prepare the risk inputs
        ruptures_per_block = self.oqparam.ruptures_per_block
        start = 0
        grp_trt = csm_info.grp_trt()
        ignore_covs = self.oqparam.ignore_covs
        for grp_id in grp_ids:
            for rupts in block_splitter(
                    ruptures_by_grp.get(grp_id, []), ruptures_per_block):
                if ignore_covs or not self.riskmodel.covs:
                    eps = None
                elif self.oqparam.asset_correlation:
                    eps = EpsilonMatrix1(num_events, self.oqparam.master_seed)
                else:
                    n_events = sum(ebr.multiplicity for ebr in rupts)
                    eps = EpsilonMatrix0(
                        len(self.assetcol), seeds[start: start + n_events])
                    start += n_events
                ri = riskinput.RiskInputFromRuptures(
                    grp_trt[grp_id], rlzs_assoc, imts, sitecol,
                    rupts, trunc_level, correl_model, min_iml, eps)
                allargs.append((ri, riskmodel, assetcol, monitor))

        self.vals = self.assetcol.values()
        taskname = '%s#%d' % (event_based_risk.__name__, sm_id + 1)
        smap = starmap(event_based_risk, allargs, name=taskname)
        attrs = dict(num_ruptures={
            sg_id: len(rupts) for sg_id, rupts in ruptures_by_grp.items()},
                     num_events=num_events,
                     num_rlzs=len(rlzs_assoc.realizations),
                     sm_id=sm_id)
        return smap, attrs

    def gen_args(self, ruptures_by_grp):
        """
        Yield the arguments required by build_ruptures, i.e. the
        source models, the asset collection, the riskmodel and others.
        """
        oq = self.oqparam
        correl_model = oq.get_correl_model()
        min_iml = self.get_min_iml(oq)
        imts = list(oq.imtls)
        ela_dt, elt_dt = build_el_dtypes(oq.insured_losses)
        csm_info = self.datastore['csm_info']
        for sm in csm_info.source_models:
            monitor = self.monitor.new(
                ses_ratio=oq.ses_ratio,
                ela_dt=ela_dt, elt_dt=elt_dt,
                loss_ratios=oq.loss_ratios,
                avg_losses=oq.avg_losses,
                insured_losses=oq.insured_losses,
                ses_per_logic_tree_path=oq.ses_per_logic_tree_path,
                maximum_distance=oq.maximum_distance,
                samples=sm.samples,
                seed=self.oqparam.random_seed)
            yield (sm.ordinal, ruptures_by_grp, self.sitecol.complete,
                   self.assetcol, self.riskmodel, imts, oq.truncation_level,
                   correl_model, min_iml, monitor)

    def execute(self):
        """
        Run the calculator and aggregate the results
        """
        if self.oqparam.ground_motion_fields:
            logging.warn('To store the ground motion fields change '
                         'calculation_mode = event_based')
        if self.oqparam.hazard_curves_from_gmfs:
            logging.warn('To compute the hazard curves change '
                         'calculation_mode = event_based')

        ruptures_by_grp = (
            self.precalc.result if self.precalc
            else event_based.get_ruptures_by_grp(self.datastore.parent))
        # the ordering of the ruptures is essential for repeatibility
        for grp in ruptures_by_grp:
            ruptures_by_grp[grp].sort(key=operator.attrgetter('serial'))
        num_rlzs = 0
        allres = []
        source_models = self.csm.info.source_models
        self.sm_by_grp = self.csm.info.get_sm_by_grp()
        for i, args in enumerate(self.gen_args(ruptures_by_grp)):
            smap, attrs = self.build_starmap(*args)
            res = smap.submit_all()
            vars(res).update(attrs)
            allres.append(res)
            res.rlz_slice = slice(num_rlzs, num_rlzs + res.num_rlzs)
            num_rlzs += res.num_rlzs
            for sg in source_models[i].src_groups:
                sg.eff_ruptures = res.num_ruptures.get(sg.id, 0)
        self.datastore['csm_info'] = self.csm.info
        num_events = self.save_results(allres, num_rlzs)
        self.save_data_transfer(parallel.IterResult.sum(allres))
        return num_events

    def save_results(self, allres, num_rlzs):
        """
        :param allres: an iterable of result iterators
        :param num_rlzs: the total number of realizations
        :returns: the total number of events
        """
        self.L = len(self.riskmodel.lti)
        self.R = num_rlzs
        self.T = len(self.assetcol.taxonomies)
        self.A = len(self.assetcol)
        ins = self.oqparam.insured_losses
        avg_losses = self.oqparam.avg_losses
        if avg_losses:
            dset = self.datastore.create_dset(
                'avg_losses-rlzs', F32, (self.A, self.R, self.L * (ins + 1)))
        num_events = 0
        self.gmfbytes = 0
        for res in allres:
            start, stop = res.rlz_slice.start, res.rlz_slice.stop
            for dic in res:
                if avg_losses:
                    for (l, r), losses in dic.pop('avglosses').items():
                        vs = self.vals[self.riskmodel.loss_types[l]]
                        dset[:, r + start, l] += losses[:, 0] * vs
                        if ins:
                            dset[:, r + start, l + self.L] += losses[:, 1] * vs
                self.gmfbytes += dic.pop('gmfbytes')
                self.save_losses(
                    dic.pop('agglosses'), dic.pop('asslosses'), start)
            logging.debug(
                'Saving results for source model #%d, realizations %d:%d',
                res.sm_id + 1, start, stop)
            if hasattr(res, 'ruptures_by_grp'):
                save_events(self, res.ruptures_by_grp)
            num_events += res.num_events
        self.datastore['events'].attrs['num_events'] = num_events
        return num_events

    def save_losses(self, agglosses, asslosses, offset):
        """
        Save the event loss tables incrementally.

        :param agglosses: a dictionary lr -> (eid, loss)
        :param asslosses: a dictionary lr -> (eid, aid, loss)
        :param offset: realization offset
        """
        with self.monitor('saving event loss tables', autoflush=True):
            for l, r in agglosses:
                loss_type = self.riskmodel.loss_types[l]
                key = 'agg_loss_table/rlz-%03d/%s' % (r + offset, loss_type)
                self.datastore.extend(key, agglosses[l, r])
            for l, r in asslosses:
                loss_type = self.riskmodel.loss_types[l]
                key = 'ass_loss_ratios/rlz-%03d/%s' % (r + offset, loss_type)
                self.datastore.extend(key, asslosses[l, r])

    def post_execute(self, num_events):
        """
        Save an array of losses by taxonomy of shape (T, L, R).
        """
        event_based.EventBasedRuptureCalculator.__dict__['post_execute'](
            self, num_events)
        if self.gmfbytes == 0:
            raise RuntimeError('No GMFs were generated, perhaps they were '
                               'all below the minimum_intensity threshold')
        logging.info('Generated %s of GMFs', humansize(self.gmfbytes))
        self.datastore.save('job_info', {'gmfbytes': self.gmfbytes})

        A, E = len(self.assetcol), num_events
        if 'ass_loss_ratios' in self.datastore:
            for rlzname in self.datastore['ass_loss_ratios']:
                self.datastore.set_nbytes('ass_loss_ratios/' + rlzname)
            self.datastore.set_nbytes('ass_loss_ratios')
            asslt = self.datastore['ass_loss_ratios']
            for rlz, dset in asslt.items():
                for ds in dset.values():
                    ds.attrs['nonzero_fraction'] = len(ds) / (A * E)

        if 'agg_loss_table' not in self.datastore:
            logging.warning(
                'No losses were generated: most likely there is an error in y'
                'our input files or the GMFs were below the minimum intensity')
        else:
            for rlzname in self.datastore['agg_loss_table']:
                self.datastore.set_nbytes('agg_loss_table/' + rlzname)
            self.datastore.set_nbytes('agg_loss_table')
            agglt = self.datastore['agg_loss_table']
            for rlz, dset in agglt.items():
                for ds in dset.values():
                    ds.attrs['nonzero_fraction'] = len(ds) / E
