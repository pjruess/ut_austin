# Paul J. Ruess
# University of Texas at Austin
# Fall, 2016

from netCDF4 import Dataset
import pandas
import scipy
import scipy.interpolate
from scipy.stats import norm
from collections import Counter
import scipy.optimize
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
import csv
import itertools
import urllib
import re
import operator

class RCData:

	def __init__(self, comid, hand_curves, hand_curves_idx, 
		hand_props, hand_props_idx, idlookup):
		"""Provides hand, xs, and usgs rating curve data for the specified comid.
		'comid' - comid for which data is desired
		'hand_curves' - NetCDF file containing HAND rating curve data
		'hand_curves_idx' - csv containing indices of HAND rating curves desired
		'hand_props' - NetCDF file containing HAND hydraulic property data
		'hand_props_idx' - csv containing indices of HAND hydraulic properties desired
		'xs' - csv containing xs data (must have profile, xsid, and rating curves)
		'idlookup' - csv lookup table between comid and usgsid"""
		self.comid = comid
		print "Retrieving data for comid {0}...".format(self.comid)

		self.hand_curves = Dataset(hand_curves, 'r')

		hand_curves_idx = pandas.read_csv(hand_curves_idx)
		self.hand_curves_idx = hand_curves_idx.loc[ hand_curves_idx['comid'] == \
			self.comid ]['index'].values[0]
		self.get_hand_curves()
		
		self.hand_props = Dataset(hand_props,'r')
		hand_props_idx = pandas.read_csv(hand_props_idx)
		self.hand_props_idx = hand_props_idx.loc[ hand_props_idx['comid'] == \
			self.comid ]['index'].values[0]
		self.get_hand_props()

		self.idlookup = idlookup

		self.usgsids = self.idlookup.loc[self.idlookup['FLComID'] == self.comid]['SOURCE_FEA'].values
		self.usgsrc = 'http://waterdata.usgs.gov/nwisweb/get_ratings?file_type=exsa&site_no={0}'

		self.get_usgsrc() # Fetch usgs stage and disch values

	def get_hand_props(self):
		"""Initializes self.handarea [sqmeters], self.handrad [m], 
		self.handslope [-], and self.handstage [ft]."""
		handc = self.hand_props.variables['COMID']
		handslope = self.hand_props.variables['Slope'] # So
		handstage = self.hand_props.variables['StageHeight'] # h values for Aw and Hr
		handarea = self.hand_props.variables['WetArea'] # Aw
		handrad = self.hand_props.variables['HydraulicRadius'] # Hr
		handlen = self.hand_props.variables['Length'] # Length
		handwidth = self.hand_props.variables['Width'] # Width
		if handc[self.hand_props_idx] == self.comid:
			self.handarea = handarea[self.hand_props_idx]*(3.28084**2) # Convert sqm to sqft
			self.handrad = handrad[self.hand_props_idx]*3.28084 # Convert m to ft
			self.handslope = handslope[self.hand_props_idx] # unitless
			self.handlen = handlen[self.hand_props_idx]*3.28084 # Convert m to ft
			self.handwidth = handwidth[self.hand_props_idx]*3.28084 # Convert m to ft
		handstage = scipy.array(handstage)*3.28084 # Convert m to ft
		self.handstage = scipy.rint(handstage) # Round to nearest int

	def get_hand_curves(self): 
		""" Initializes self.handq [cfs] and self.handh [ft]."""
		handq = self.hand_curves.variables['Q_cfs']
		handh = self.hand_curves.variables['H_ft']
		handc = self.hand_curves.variables['COMID']
		if handc[self.hand_curves_idx] == self.comid:
			self.handq = handq[self.hand_curves_idx]
		self.handh = handh

	def get_usgsrc(self):
		""" Initializes self.usgsq and self.usgsh """
		self.usgsh = []
		self.usgsq = []
		for usgsid in self.usgsids:
			urlfile = urllib.urlopen(self.usgsrc.format(str(usgsid)))
			urllines = urlfile.readlines()
			findData = False
			usgsq = scipy.array([])
			usgsh = scipy.array([])
			for j in range(len(urllines)):
				line = urllines[j]
				if not findData and not re.search('[a-zA-Z]',line): # No letters
					findData = True
				if findData and float(line.split('\t')[2]) >= 1: # Remove where Q < 1
					current = line.split('\t')
					usgsq = scipy.append( usgsq, float(current[2]) )
					# apply shift to stage height where current[1] is shift magnitude
					usgsh = scipy.append( usgsh, float(current[0]) - float(current[1]) )
			shift = usgsh[0]
			self.usgsh.append((usgsh - shift)) # Normalize usgsh over bottom depth
			self.usgsq.append(usgsq)
		self.usgsh = scipy.array(self.usgsh)
		self.usgsq = scipy.array(self.usgsq)

class RCDist(RCData):

	def interp(self,x,y,kind='power'):
		"""Interpolate over data with (x,y) pairs
		'x' - x data,
		'y' - y data,
		'kind' - powerlaw ('power'), linear ('linear), or cubic ('cubic')"""
		if kind == 'power': # powerlaw interpolation
			logx = scipy.log(x)[1:] # ln(0) is neg inf, so remove first term
			logy = scipy.log(y)[1:] # ln(0) is neg inf, so remove first term
			b, loga = scipy.polyfit(logx,logy,1) # slope, intercept from (y = a + b*x)
			a = scipy.exp(loga)
			f = lambda x: a * x**b # powerlaw function

		if kind == 'linear' or kind == 'cubic': # linear interpolation
			f = scipy.interpolate.interp1d(x,y,kind=kind)

		return f

	def mannings_n(self,area,hydrad,slope,disch):
		""" Calculates manning's roughness from discharge. 
		'area' - self.handarea (wet area),
		'hydrad' - self.handrad (hydraulic radius),
		'slope' - self.handslope (bed slope), and
		'disch' - any discharge values"""
		res = 1.49*area*scipy.power(hydrad,(2/3.0))*scipy.sqrt(slope)/disch.T
		return res.T

	def optimize_n(self):
		"""Determine manning's roughness required to fit
		HAND curve to USGS curve at each 1-ft depth interval"""
		usgs_hlist = []
		usgs_hset = set()
		usgs_qlist = []
		for h in self.handstage: 
			res = min(enumerate(self.usgsh[0]), key=lambda x:abs(x[1]-h))
			if res[1] not in usgs_hset:
				usgs_hlist.append(res[1])
				usgs_hset.add(res[1])
				usgs_qlist.append(self.usgsq[0][res[0]])
		usgs_hlist = scipy.array(usgs_hlist[:-1])
		usgs_qlist = scipy.array(usgs_qlist[:-1])
		area = self.handarea[:len(usgs_qlist)]
		hydrad = self.handrad[:len(usgs_qlist)]
		opt_n = self.mannings_n(area=area,hydrad=hydrad,slope=self.handslope,disch=usgs_qlist)
		return zip(usgs_hlist,usgs_qlist,opt_n)

	def get_usgs_geometry(self,usgsid):
		""" Retrieves USGS geometry data """

		weburl = 'https://waterdata.usgs.gov/tx/nwis/measurements?site_no={0}&agency_cd=USGS&format=rdb_expanded'

		# Retrieve data
		urlfile = urllib.urlopen(weburl.format(str(usgsid)))
		urllines = urlfile.readlines()
		urllines = [line.split('\t') for line in urllines if line[0] != '#'] # Ignore details at beginning
		del urllines[1] # Remove additional unnecessary details

		# Separate headers and data
		keys = urllines[0]
		values = urllines[1:]

		d = {k:list(v) for k,v in zip(keys,zip(*values))}
		return d

	def draw_xsect(self,save=False):

		# Initiate figures and axes
		fig, ax = plt.subplots()

		# Create and draw HAND cross-section polygon
		# Generate origin for plotting (note: must be done prior to for loop)
		hand_xsect = scipy.array([[0,0]])

		for h in range(len(self.handstage)):
			# Retrieve top-width data for this height step
			delta_w = self.handwidth[h]/2.0

			# Collect negative and positive widths for this height step
			neg = scipy.array([[-delta_w,h]])
			pos = scipy.array([[delta_w,h]])

			# Organize final data as LHS, origin, RHS
			hand_xsect = scipy.concatenate([neg,hand_xsect,pos])

		# Draw HAND cross-section
		hand_poly = plt.Polygon(hand_xsect,closed=None,fill=None,edgecolor='b',
			linewidth=5,label='HAND X-Sect')
		ax.add_artist(hand_poly)

		usgs_max_height = 0
		usgs_max_width = 0

		# Create and draw USGS cross-section polygon
		for usgsid in self.usgsids:
			# Generate origin for plotting (note: must be done within for loop)
			usgs_xsect = scipy.array([[0,0]])

			# Retrieve dictionary with USGS data
			d = self.get_usgs_geometry(usgsid)
			
			# Collect indices of most recent rating number only
			ratings = [(ind,float(r)) for ind,r in enumerate(d['current_rating_nu']) if filter(None,r)]

			print usgsid

			# Find index of latest occurence of most recent rating number
			rnos = zip(*ratings)[1]
			most_recent = ratings[rnos.index(rnos[-1])][0]

			# Collect height and width data (note: divide width by 2 for one-sided width), 
			# while removing pairs missing one element and taking only most recent rating number
			ratings = [float(r) for r in d['current_rating_nu'] if filter(None,r)]

			data = [(float(w)/2.0,float(h))\
				for w,h,r in zip(d['chan_width'],d['gage_height_va'],d['current_rating_nu'])\
				if filter(None,w) and filter(None,h) and filter(None,r) and float(r) == ratings[-1]]
			print data

			# data = [filter(None,t) for t in zip(d['gage_height_va'],d['chan_width'])]

			# Sort data: ascending height and ascending width
			pos = scipy.array(sorted(data,key=operator.itemgetter(1,0))) 

			# Sort data: ascending height and descending width
			neg = scipy.array(sorted(data,key=operator.itemgetter(1,0),reverse=True)) 
			neg[:,0] = -neg[:,0] # change widths to negative for plotting

			# Organize final data as LHS, origin, RHS
			usgs_xsect = scipy.concatenate([neg,usgs_xsect,pos])

			# Draw USGS cross-section
			usgs_poly = plt.Polygon(usgs_xsect,closed=None,fill=None,edgecolor='g',
				linewidth=5,label='USGS X-Sect')
			ax.add_artist(usgs_poly)

		# Customize plot
		fig.set_size_inches(20,16, forward=True)
		plt.gca().set_xlim(left=-self.handwidth[-1],right=self.handwidth[-1])
		plt.gca().set_ylim(bottom=self.handstage[0],top=self.handstage[-1])
		
		# Manually over-ride HAND limits
		plt.gca().set_xlim(left=-self.handwidth[11],right=self.handwidth[11])
		plt.gca().set_ylim(bottom=-1,top=self.handstage[11])

		ax.set_xticks(ax.get_xticks()[::2])
		ax.set_yticks(ax.get_yticks()[::2])
		title = 'COMID {0}'.format(self.comid)
		ax.set_title(title, y=1.04, fontsize=56)
		plt.xlabel('Width (ft)',fontsize=56)
		plt.ylabel('Height (ft)',fontsize=56)
		plt.rc('font', size=56)
		# plt.legend(loc='upper left',fontsize=40)
		plt.legend([hand_poly, usgs_poly], ['hand', 'usgs'])
		plt.tick_params(axis='both',labelsize=56)
		plt.grid()

		if save:
			fig.savefig(save)
			plt.clf()

		if not save: 
			# mng = plt.get_current_fig_manager()
			# mng.resize(*mng.window.maxsize())
			plt.show()
			plt.clf()

	def plot_rc(self,save=False,hand=True,usgs=True,
		dist=5000,kind='power',alpha=0.05,div=5):
		"""Plot HAND and xs rating curves with confidence intervals
		'hand' - plot hand rating curve [T/F]
		'xs' - plot xs rating curves [T/F]
		'xsapprox' - plot xs rating curve approximation from n-value averages [T/F]
		'ci' - plot confidence intervals [T/F]
		'alpha' - alpha for confidence intervals [float(0.0,1.0)]
		'div' - number of intervals for confidence interval [R]"""

		fig, ax = plt.subplots() # get figure and axes for plotting

		if usgs: # Plot interpolated USGS rating curve
			# Plot curves
			for q,h in zip(self.usgsq,self.usgsh):
				if kind == 'cubic': 
					print 'USGS interpolation plotted as power-law fit'
					f = self.interp(x=q,y=h,kind='power')
				else: 
					f = self.interp(x=q,y=h,kind=kind)
				ax.plot(q,f(q),
					label='usgs',c='g', linewidth=5)

		if hand: # Plot interpolated HAND rating curve
			# Plot curves
			f = self.interp(x=self.handq,y=self.handh,kind=kind)
			ax.plot(self.handq,f(self.handq),
				label='hand',c='b', linewidth=5)

		# Plot graph
		fig.set_size_inches(20,16, forward=True)
		plt.gca().set_xlim(left=0,right=self.handq[-1])
		plt.gca().set_ylim(bottom=0,top=self.handh[-1])
		ax.set_xticks(ax.get_xticks()[::2])
		ax.set_yticks(ax.get_yticks()[::2])
		title = 'COMID {0}'.format(self.comid)
		ax.set_title(title, y=1.04, fontsize=56)
		plt.xlabel('Q (cfs)',fontsize=56)
		plt.ylabel('H (ft)',fontsize=56)
		ax.ticklabel_format(style='sci',axis='x',scilimits=(0,0))
		plt.rc('font', size=56)
		plt.legend(loc='upper left',fontsize=40)
		plt.tick_params(axis='both',labelsize=56)
		plt.grid()

		if save:
			fig.savefig(fname)
			plt.clf()

		if not save: 
			# mng = plt.get_current_fig_manager()
			# mng.resize(*mng.window.maxsize())
			plt.show()
			plt.clf()

if __name__ == '__main__':
	
	# Path to HAND files
	hand_curves = 'oniondata/handratingcurves.nc'
	hand_curves_idx = 'oniondata/handrc_idx.csv'
	hand_props = 'oniondata/OnionCreek.nc'
	hand_props_idx = 'oniondata/handnc_idx.csv'

	# Pre-process XS data
	xsintersect = pandas.read_csv('oniondata/xsdata/xsintersect.csv',
		usecols=['COMID','ProfileM','RiverStation'])
	xsintersect = xsintersect.astype(str)
	xsrating = pandas.read_csv('oniondata/xsdata/xsrating.csv',
		usecols=['Stage_Height_ft_','Discharge_cfs_','RiverStation','XSID'])
	xsrating = xsrating.astype(str)
	xs = pandas.merge(xsrating,xsintersect,on='RiverStation')

	# Pre-process id lookup table (USGSID <--> COMID)	
	idlookup = pandas.read_csv('oniondata/streamgages.csv',usecols=['SOURCE_FEA','FLComID'])
	idlookup['SOURCE_FEA'] = idlookup['SOURCE_FEA'].apply(lambda x: '{0:0>8}'.format(x))
	
	# Get lists of COMIDs to iterate over
	comids = idlookup.FLComID.unique()

	# Get all COMIDs in onion creek
	nhd = pandas.read_csv('oniondata/onion_nhd.csv')
	# FUTURE WORK - Get all USGSIDs that correlate

	# Override with reasonable-looking comids (from multi-boxplots)
	comids = [5781373,5781407]
	# comids = [5781369]

	# Instantiate RCDist class for each comid in watershed
	for comid in comids:
		try: 
			rcdist = RCDist(comid,hand_curves, hand_curves_idx, 
				hand_props,hand_props_idx,idlookup)
			print 'COMID {0} Collected Successfully!\n'.format(str(comid))
			# print 'usgsid:',[usgsid for usgsid in rcdist.usgsids]
			# rcdist.draw_xsect(save='results/xsects/{0}'.format(str(comid)))
			rcdist.draw_xsect(save='results/xsects/hand_vs_usgs_xsect_recent_ratings_only_trimmed_{0}'.format(str(comid)))

			# print rcdist.optimize_n()

			# Plot rating curves from data
			# rcdist.plot_rc(save=False,hand=True,usgs=True,
			# 	dist=100000000,kind='linear',alpha=0.05,div=5)

			continue
		except TypeError: 
			print 'COMID {0} XS Error\n'.format(str(comid))
			continue
		except IndexError: 
			print 'COMID {0} RC Error\n'.format(str(comid))
			continue